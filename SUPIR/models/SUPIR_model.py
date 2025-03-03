import torch
from sgm.models.diffusion import DiffusionEngine
from sgm.util import instantiate_from_config
import copy
from sgm.modules.distributions.distributions import DiagonalGaussianDistribution
import random
from SUPIR.utils.colorfix import wavelet_reconstruction, adaptive_instance_normalization
from pytorch_lightning import seed_everything
from torch.nn.functional import interpolate

class SUPIRModel(DiffusionEngine):
    def __init__(self, control_stage_config, ae_dtype='fp32', diffusion_dtype='fp32', p_p='', n_p='', *args, **kwargs):
        super().__init__(*args, **kwargs)
        control_model = instantiate_from_config(control_stage_config)
        self.model.load_control_model(control_model)
        self.first_stage_model.denoise_encoder = copy.deepcopy(self.first_stage_model.encoder)
        self.sampler_config = kwargs['sampler_config']

        assert (ae_dtype in ['fp32', 'fp16', 'bf16']) and (diffusion_dtype in ['fp32', 'fp16', 'bf16'])
        if ae_dtype == 'fp32':
            ae_dtype = torch.float32
        elif ae_dtype == 'fp16':
            raise RuntimeError('fp16 cause NaN in AE')
        elif ae_dtype == 'bf16':
            ae_dtype = torch.bfloat16

        if diffusion_dtype == 'fp32':
            diffusion_dtype = torch.float32
        elif diffusion_dtype == 'fp16':
            diffusion_dtype = torch.float16
        elif diffusion_dtype == 'bf16':
            diffusion_dtype = torch.bfloat16

        self.ae_dtype = ae_dtype
        self.model.dtype = diffusion_dtype

        self.p_p = p_p
        self.n_p = n_p

    @torch.no_grad()
    def encode_first_stage(self, x):
        with torch.autocast("cuda", dtype=self.ae_dtype):
            z = self.first_stage_model.encode(x)
        z = self.scale_factor * z
        return z

    @torch.no_grad()
    def encode_first_stage_with_denoise(self, x, use_sample=True):
        with torch.autocast("cuda", dtype=self.ae_dtype):
            h = self.first_stage_model.denoise_encoder(x)
            moments = self.first_stage_model.quant_conv(h)
            posterior = DiagonalGaussianDistribution(moments)
            if use_sample:
                z = posterior.sample()
            else:
                z = posterior.mode()
        z = self.scale_factor * z
        return z

    @torch.no_grad()
    def decode_first_stage(self, z):
        z = 1.0 / self.scale_factor * z
        with torch.autocast("cuda", dtype=self.ae_dtype):
            out = self.first_stage_model.decode(z)
        return out.float()

    @torch.no_grad()
    def batchify_denoise(self, x):
        '''
        [N, C, H, W], [-1, 1], RGB
        '''
        x = self.encode_first_stage_with_denoise(x, use_sample=False)
        return self.decode_first_stage(x)

    @torch.no_grad()
    def batchify_sample(self, x, p, p_p='default', n_p='default', num_steps=100, restoration_scale=4.0, s_churn=0, s_noise=1.003, cfg_scale=4.0, seed=-1,
                        num_samples=1, control_scale=1, color_fix_type='None', use_linear_CFG=False, use_linear_control_scale=False,
                        cfg_scale_start=1.0, control_scale_start=0.0, **kwargs):
        '''
        [N, C], [-1, 1], RGB
        '''
        assert len(x) == len(p)
        assert color_fix_type in ['Wavelet', 'AdaIn', 'None']

        N = len(x)
        if num_samples > 1:
            assert N == 1
            N = num_samples
            x = x.repeat(N, 1, 1, 1)
            p = p * N

        if p_p == 'default':
            p_p = self.p_p
        if n_p == 'default':
            n_p = self.n_p

        self.sampler_config.params.num_steps = num_steps
        if use_linear_CFG:
            self.sampler_config.params.guider_config.params.scale_min = cfg_scale
            self.sampler_config.params.guider_config.params.scale = cfg_scale_start
        else:
            self.sampler_config.params.guider_config.params.scale = cfg_scale
        self.sampler_config.params.restore_cfg = restoration_scale
        self.sampler_config.params.s_churn = s_churn
        self.sampler_config.params.s_noise = s_noise
        self.sampler = instantiate_from_config(self.sampler_config)

        if seed == -1:
            seed = random.randint(0, 65535)
        seed_everything(seed)

        _z = self.encode_first_stage_with_denoise(x, use_sample=False)

        x_stage1 = self.decode_first_stage(_z)
        # x_stage1 = interpolate(x_stage1, scale_factor=scale_factor, mode='bilinear', antialias=True)
        # _z = self.encode_first_stage_with_denoise(x_stage1)

        z_stage1 = self.encode_first_stage(x_stage1)

        batch = {}
        batch['txt'] = [''.join([_p, p_p]) for _p in p]
        batch['original_size_as_tuple'] = torch.tensor([1024, 1024]).repeat(N, 1).to(x.device)
        batch['crop_coords_top_left'] = torch.tensor([0, 0]).repeat(N, 1).to(x.device)
        batch['target_size_as_tuple'] = torch.tensor([1024, 1024]).repeat(N, 1).to(x.device)
        batch['aesthetic_score'] = torch.tensor([9.0]).repeat(N, 1).to(x.device)
        batch['control'] = _z

        batch_uc = copy.deepcopy(batch)
        batch_uc['txt'] = [n_p for _ in p]

        c, uc = self.conditioner.get_unconditional_conditioning(batch, batch_uc)

        denoiser = lambda input, sigma, c, control_scale: self.denoiser(
            self.model, input, sigma, c, control_scale, **kwargs
        )

        noised_z = torch.randn_like(_z).to(_z.device)

        _samples = self.sampler(denoiser, noised_z, cond=c, uc=uc, x_center=z_stage1, control_scale=control_scale,
                                use_linear_control_scale=use_linear_control_scale, control_scale_start=control_scale_start)
        samples = self.decode_first_stage(_samples)
        if color_fix_type == 'Wavelet':
            samples = wavelet_reconstruction(samples, x_stage1)
        elif color_fix_type == 'AdaIn':
            samples = adaptive_instance_normalization(samples, x_stage1)
        return samples


if __name__ == '__main__':
    from SUPIR.util import create_model, load_state_dict

    model = create_model('../../options/dev/SUPIR_paper_version.yaml')

    SDXL_CKPT = '/opt/data/private/AIGC_pretrain/SDXL_cache/sd_xl_base_1.0_0.9vae.safetensors'
    SUPIR_CKPT = '/opt/data/private/AIGC_pretrain/SUPIR_cache/SUPIR-paper.ckpt'
    model.load_state_dict(load_state_dict(SDXL_CKPT), strict=False)
    model.load_state_dict(load_state_dict(SUPIR_CKPT), strict=False)
    model = model.cuda()

    x = torch.randn(1, 3, 512, 512).cuda()
    p = ['a professional, detailed, high-quality photo']
    samples = model.batchify_sample(x, p, num_steps=50, restoration_scale=4.0, s_churn=0, cfg_scale=4.0, seed=-1, num_samples=1)
