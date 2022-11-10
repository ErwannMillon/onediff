import inspect
import warnings
from typing import List, Optional, Union

import numpy as np
import oneflow as torch

import PIL
from transformers import CLIPFeatureExtractor, CLIPTokenizer
from transformers import OneFlowCLIPTextModel as CLIPTextModel

from ...configuration_utils import FrozenDict
from ...models import OneFlowAutoencoderKL as AutoencoderKL
from ...models import OneFlowUNet2DConditionModel as UNet2DConditionModel
from ...pipeline_oneflow_utils import OneFlowDiffusionPipeline as DiffusionPipeline
from ...schedulers import OneFlowDDIMScheduler as DDIMScheduler
from ...schedulers import OneFlowPNDMScheduler as PNDMScheduler
from ...schedulers import LMSDiscreteScheduler
from . import StableDiffusionPipelineOutput
from .safety_checker_oneflow import OneFlowStableDiffusionSafetyChecker as StableDiffusionSafetyChecker

import os
os.environ["ONEFLOW_MLIR_ENABLE_ROUND_TRIP"] = "1"
os.environ["ONEFLOW_MLIR_ENABLE_INFERENCE_OPTIMIZATION"] = "1"
os.environ["ONEFLOW_MLIR_PREFER_NHWC"] = "1"
os.environ["ONEFLOW_KERNEL_ENABLE_CUDNN_FUSED_CONV_BIAS"] = "1"
os.environ["ONEFLOW_KERNEL_ENABLE_FUSED_LINEAR"] = "1"

def preprocess(image):
    w, h = image.size
    w, h = map(lambda x: x - x % 32, (w, h))  # resize to integer multiple of 32
    image = image.resize((w, h), resample=PIL.Image.LANCZOS)
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2.0 * image - 1.0


import oneflow as flow
class UNetGraph(flow.nn.Graph):
    def __init__(self, unet):
        super().__init__()
        self.unet = unet
        self.config.enable_cudnn_conv_heuristic_search_algo(False)

    def build(self, latent_model_input, t, text_embeddings):
        text_embeddings = torch._C.amp_white_identity(text_embeddings)
        return self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample


class OneFlowStableDiffusionImg2ImgPipeline(DiffusionPipeline):

    def __init__(
        self,
        vae: AutoencoderKL,
        text_encoder: CLIPTextModel,
        tokenizer: CLIPTokenizer,
        unet: UNet2DConditionModel,
        scheduler: Union[DDIMScheduler, PNDMScheduler, LMSDiscreteScheduler],
        safety_checker: StableDiffusionSafetyChecker,
        feature_extractor: CLIPFeatureExtractor,
    ):
        super().__init__()
        scheduler = scheduler.set_format("pt")

        if hasattr(scheduler.config, "steps_offset") and scheduler.config.steps_offset != 1:
            warnings.warn(
                f"The configuration file of this scheduler: {scheduler} is outdated. `steps_offset`"
                f" should be set to 1 instead of {scheduler.config.steps_offset}. Please make sure "
                "to update the config accordingly as leaving `steps_offset` might led to incorrect results"
                " in future versions. If you have downloaded this checkpoint from the Hugging Face Hub,"
                " it would be very nice if you could open a Pull request for the `scheduler/scheduler_config.json`"
                " file",
                DeprecationWarning,
            )
            new_config = dict(scheduler.config)
            new_config["steps_offset"] = 1
            scheduler._internal_dict = FrozenDict(new_config)

        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler,
            safety_checker=safety_checker,
            feature_extractor=feature_extractor,
        )
        self.unet_graphs = dict()
        self.unet_graphs_cache_size = 1
        self.unet_graphs_lru_cache_time = 0

    def enable_attention_slicing(self, slice_size: Optional[Union[str, int]] = "auto"):
        r"""
        Enable sliced attention computation.

        When this option is enabled, the attention module will split the input tensor in slices, to compute attention
        in several steps. This is useful to save some memory in exchange for a small speed decrease.

        Args:
            slice_size (`str` or `int`, *optional*, defaults to `"auto"`):
                When `"auto"`, halves the input to the attention heads, so attention will be computed in two steps. If
                a number is provided, uses as many slices as `attention_head_dim // slice_size`. In this case,
                `attention_head_dim` must be a multiple of `slice_size`.
        """
        if slice_size == "auto":
            # half the attention head size is usually a good trade-off between
            # speed and memory
            slice_size = self.unet.config.attention_head_dim // 2
        self.unet.set_attention_slice(slice_size)

    def disable_attention_slicing(self):
        r"""
        Disable sliced attention computation. If `enable_attention_slicing` was previously invoked, this method will go
        back to computing attention in one step.
        """
        # set slice_size = `None` to disable `set_attention_slice`
        self.enable_attention_slicing(None)

    def set_unet_graphs_cache_size(self, cache_size: int):
        r"""
        Set the cache size of compiled unet graphs.

        This option is designed to control the GPU memory size.

        Args:
            cache_size ([`int`]):
                New cache size, i.e., the maximum number of unet graphs.
        """
        self.unet_graphs_cache_size = cache_size

    @torch.no_grad()
    def __call__(
        self,
        prompt: Union[str, List[str]],
        init_image: Union[torch.FloatTensor, PIL.Image.Image],
        strength: float = 0.8,
        num_inference_steps: Optional[int] = 50,
        guidance_scale: Optional[float] = 7.5,
        eta: Optional[float] = 0.0,
        generator: Optional[torch.Generator] = None,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        compile_unet: bool = True,
    ):
        r"""
        Function invoked when calling the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`):
                The prompt or prompts to guide the image generation.
            init_image (`torch.FloatTensor` or `PIL.Image.Image`):
                `Image`, or tensor representing an image batch, that will be used as the starting point for the
                process.
            strength (`float`, *optional*, defaults to 0.8):
                Conceptually, indicates how much to transform the reference `init_image`. Must be between 0 and 1.
                `init_image` will be used as a starting point, adding more noise to it the larger the `strength`. The
                number of denoising steps depends on the amount of noise initially added. When `strength` is 1, added
                noise will be maximum and the denoising process will run for the full number of iterations specified in
                `num_inference_steps`. A value of 1, therefore, essentially ignores `init_image`.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference. This parameter will be modulated by `strength`.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) in the DDIM paper: https://arxiv.org/abs/2010.02502. Only applies to
                [`schedulers.DDIMScheduler`], will be ignored for others.
            generator (`torch.Generator`, *optional*):
                A [torch generator](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make generation
                deterministic.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generate image. Choose between
                [PIL](https://pillow.readthedocs.io/en/stable/): `PIL.Image.Image` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
                plain tuple.
            compile_unet (`bool`, *optional*, defaults to `True`):
                Whether or not to compile unet as nn.graph

        Returns:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] if `return_dict` is True, otherwise a `tuple.
            When returning a tuple, the first element is a list with the generated images, and the second element is a
            list of `bool`s denoting whether the corresponding generated image likely represents "not-safe-for-work"
            (nsfw) content, according to the `safety_checker`.
        """
        if isinstance(prompt, str):
            batch_size = 1
        elif isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            raise ValueError(f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")

        if strength < 0 or strength > 1:
            raise ValueError(f"The value of strength should in [0.0, 1.0] but is {strength}")

        # set timesteps
        self.scheduler.set_timesteps(num_inference_steps)

        if isinstance(init_image, PIL.Image.Image):
            init_image = preprocess(init_image)

        # encode the init image into latents and scale the latents
        init_latent_dist = self.vae.encode(init_image.to(self.device)).latent_dist
        init_latents = init_latent_dist.sample(generator=generator)
        init_latents = 0.18215 * init_latents

        # expand init_latents for batch_size
        init_latents = torch.cat([init_latents] * batch_size)

        # get the original timestep using init_timestep
        offset = self.scheduler.config.get("steps_offset", 0)
        init_timestep = int(num_inference_steps * strength) + offset
        init_timestep = min(init_timestep, num_inference_steps)
        if isinstance(self.scheduler, LMSDiscreteScheduler):
            timesteps = torch.tensor(
                [num_inference_steps - init_timestep] * batch_size, dtype=torch.long, device=self.device
            )
        else:
            timesteps = self.scheduler.timesteps[-init_timestep]
            timesteps = torch.tensor([timesteps] * batch_size, dtype=torch.long, device=self.device)

        # add noise to latents using the timesteps
        noise = torch.randn(init_latents.shape, generator=generator, device=self.device)
        init_latents = self.scheduler.add_noise(init_latents, noise, timesteps)

        # get prompt text embeddings
        text_input = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="np",
        )
        text_input.input_ids = torch.from_numpy(text_input.input_ids)
        text_embeddings = self.text_encoder(text_input.input_ids.to(self.device))[0]

        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0
        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance:
            max_length = text_input.input_ids.shape[-1]
            uncond_input = self.tokenizer(
                [""] * batch_size, padding="max_length", max_length=max_length, return_tensors="np"
            )
            uncond_input.input_ids = torch.from_numpy(uncond_input.input_ids)
            uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(self.device))[0]

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]
        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        latents = init_latents

        compilation_start = timer()
        compilation_time = 0
        if compile_unet:
            self.unet_graphs_lru_cache_time += 1
            if (height, width) in self.unet_graphs:
                _, unet_graph = self.unet_graphs[height, width]
                self.unet_graphs[height, width] = (self.unet_graphs_lru_cache_time, unet_graph)
            else:
                while len(self.unet_graphs) >= self.unet_graphs_cache_size:
                    shape_to_del = min(self.unet_graphs.keys(), key=lambda shape: self.unet_graphs[shape][0])
                    print("[oneflow]", f"a compiled unet (height={shape_to_del[0]}, width={shape_to_del[1]}) "
                          "is deleted according to the LRU policy")
                    print("[oneflow]", "cache size can be changed by `pipeline.set_unet_graphs_cache_size`")
                    del self.unet_graphs[shape_to_del]
                print("[oneflow]", "compiling unet beforehand to make sure the progress bar is more accurate")
                i, t = list(enumerate(self.scheduler.timesteps))[0]
                latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
                unet_graph = UNetGraph(self.unet)
                unet_graph._compile(latent_model_input, t, text_embeddings)
                unet_graph(latent_model_input, t, text_embeddings) # warmup
                compilation_time = timer() - compilation_start
                print("[oneflow]", "[elapsed(s)]", "[unet compilation]", compilation_time)
                self.unet_graphs[height, width] = (self.unet_graphs_lru_cache_time, unet_graph)


        t_start = max(num_inference_steps - init_timestep + offset, 0)
        for i, t in enumerate(self.progress_bar(self.scheduler.timesteps[t_start:])):
            t_index = t_start + i

            # expand the latents if we are doing classifier free guidance
            latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents

            # if we use LMSDiscreteScheduler, let's make sure latents are multiplied by sigmas
            if isinstance(self.scheduler, LMSDiscreteScheduler):
                sigma = self.scheduler.sigmas[t_index]
                # the model input needs to be scaled to match the continuous ODE formulation in K-LMS
                latent_model_input = latent_model_input / ((sigma**2 + 1) ** 0.5)

            # predict the noise residual
            if compile_unet:
                noise_pred = unet_graph(latent_model_input, t, text_embeddings)
            else:
                # predict the noise residual
                noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample

            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            if isinstance(self.scheduler, LMSDiscreteScheduler):
                latents = self.scheduler.step(noise_pred, t_index, latents, **extra_step_kwargs).prev_sample
            else:
                latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs).prev_sample

        # scale and decode the image latents with vae
        latents = 1 / 0.18215 * latents
        image = self.vae.decode(latents).sample

        print("[oneflow]", "[elapsed(s)]", "[image]", timer() - start - compilation_time)
        post_process_start = timer()

        image = (image / 2 + 0.5).clamp(0, 1)
        image = image.cpu().permute(0, 2, 3, 1).numpy()

        # run safety checker
        safety_checker_input = self.feature_extractor(self.numpy_to_pil(image), return_tensors="np")
        safety_checker_input.pixel_values = torch.from_numpy(safety_checker_input.pixel_values).to(self.device)
        torch._oneflow_internal.profiler.RangePush(f"safety-checker")
        image, has_nsfw_concept = self.safety_checker(images=image, clip_input=safety_checker_input.pixel_values)
        torch._oneflow_internal.profiler.RangePop()

        if output_type == "pil":
            image = self.numpy_to_pil(image)

        if not return_dict:
            return (image, has_nsfw_concept)

        print("[oneflow]", "[elapsed(s)]", "[post-process]", timer() - post_process_start)

        return StableDiffusionPipelineOutput(images=image, nsfw_content_detected=has_nsfw_concept)
