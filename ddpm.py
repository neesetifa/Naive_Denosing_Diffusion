import os
import torch
import torch.nn as nn
from matplotlib import pyplot as plt
from tqdm import tqdm
from torch import optim
from utils import *
from modules import UNet
import logging
from torch.utils.tensorboard import SummaryWriter
from dataset import CustomDataset
import pdb
import numpy as np

logging.basicConfig(format="%(asctime)s - %(levelname)s: %(message)s", level=logging.INFO, datefmt="%I:%M:%S")


# beta schedule
def linear_beta_schedule(timesteps):
    scale = 1000 / timesteps
    beta_start = scale * 0.0001
    beta_end = scale * 0.02
    return torch.linspace(beta_start, beta_end, timesteps, dtype=torch.float64)

def cosine_beta_schedule(timesteps, s=0.008):
    """
    cosine schedule
    as proposed in https://arxiv.org/abs/2102.09672
    """
    steps = timesteps + 1
    x = torch.linspace(0, timesteps, steps, dtype=torch.float64)
    alphas_cumprod = torch.cos(((x / timesteps) + s) / (1 + s) * math.pi * 0.5) ** 2
    alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
    betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
    return torch.clip(betas, 0, 0.999)

class Diffusion:
    def __init__(self, noise_steps=1000, beta_schedule='linear', img_size=256, device="cuda"):
        self.noise_steps = noise_steps
        self.img_size = img_size
        self.device = device

        self.beta = self.prepare_noise_schedule(beta_schedule).to(device)
        self.alpha = 1. - self.beta
        self.alpha_hat = torch.cumprod(self.alpha, dim=0)
        
    def prepare_noise_schedule(self, beta_schedule):
        if beta_schedule == 'linear':
            return linear_beta_schedule(self.noise_steps)
        elif beta_schedule == 'cosine':
            return cosine_beta_schedule(self.noise_steps)
        else:
            raise ValueError(f'Currently only support beta schedule to be "linear" or "cosine", but got {beta_schedule}')

    def noise_images(self, x, t):
        sqrt_alpha_hat = torch.sqrt(self.alpha_hat[t])[:, None, None, None]
        sqrt_one_minus_alpha_hat = torch.sqrt(1 - self.alpha_hat[t])[:, None, None, None]
        Ɛ = torch.randn_like(x)
        return sqrt_alpha_hat * x + sqrt_one_minus_alpha_hat * Ɛ, Ɛ

    def sample_timesteps(self, n):
        return torch.randint(low=1, high=self.noise_steps, size=(n,))

    def sample(self, model, n):
        logging.info(f"Sampling {n} new images....")
        model.eval()
        with torch.no_grad():
            x = torch.randn((n, 3, self.img_size, self.img_size)).to(self.device)
            for i in tqdm(reversed(range(1, self.noise_steps)), position=0):
                t = (torch.ones(n) * i).long().to(self.device)
                predicted_noise = model(x, t)
                alpha = self.alpha[t][:, None, None, None]
                alpha_hat = self.alpha_hat[t][:, None, None, None]
                beta = self.beta[t][:, None, None, None]
                if i > 1:
                    noise = torch.randn_like(x)
                else:
                    noise = torch.zeros_like(x)
                x = 1 / torch.sqrt(alpha) * (x - ((1 - alpha) / (torch.sqrt(1 - alpha_hat))) * predicted_noise) + torch.sqrt(beta) * noise
        model.train()
        x = (x.clamp(-1, 1) + 1) / 2
        x = (x * 255).type(torch.uint8)
        return x  # [batch, 3, H, W]

    def _extract(self, a, t, x_shape):
        batch_size = t.shape[0]
        out = a.to(t.device).gather(0, t).float()
        out = out.reshape(batch_size, *((1,) * (len(x_shape) - 1)))
        return out
    
    def sample_ddim(self, model, n, ddim_timesteps=50, ddim_discr_method="uniform", ddim_eta=0.0, clip_denoised=True):
        # make ddim timestep sequence
        if ddim_discr_method == 'uniform':
            c = self.noise_steps // ddim_timesteps
            ddim_timestep_seq = np.asarray(list(range(0, self.noise_steps, c)))
        elif ddim_discr_method == 'quad':
            ddim_timestep_seq = ((np.linspace(0, np.sqrt(self.noise_steps * .8), ddim_timesteps)) ** 2).astype(int)
        else:
            raise NotImplementedError(f'There is no ddim discretization method called "{ddim_discr_method}"')
        # add one to get the final alpha values right (the ones from first scale to data during sampling)
        ddim_timestep_seq = ddim_timestep_seq + 1
        # previous sequence
        ddim_timestep_prev_seq = np.append(np.array([0]), ddim_timestep_seq[:-1])

        logging.info(f"Sampling {n} new images....")
        model.eval()
        with torch.no_grad():
            x = torch.randn((n, 3, self.img_size, self.img_size)).to(self.device) # start from x_{t}
            for i in tqdm(reversed(range(0, ddim_timesteps)), desc='sampling loop time step', total=ddim_timesteps):
                t = torch.full((n,), ddim_timestep_seq[i], device=self.device, dtype=torch.long)
                prev_t = torch.full((n,), ddim_timestep_prev_seq[i], device=self.device, dtype=torch.long)
                
                # 1. get current and previous alpha_cumprod
                alpha_hat_t = self._extract(self.alpha_hat, t, x.shape)
                alpha_hat_t_prev = self._extract(self.alpha_hat, prev_t, x.shape)
                
                # 2. predict noise using model with x_{t} and time t
                predicted_noise = model(x, t)

                # 3. Now let's calculate formula (12) one by one
                # (1) get the predicted x_0
                pred_x0 = (x - torch.sqrt((1. - alpha_hat_t)) * predicted_noise) / torch.sqrt(alpha_hat_t)
                if clip_denoised:
                    pred_x0 = pred_x0.clamp(-1., 1.)
            
                # (2) compute variance: "sigma_t(η)" -> see formula (16)
                # σ_t = sqrt((1 − α_t−1)/(1 − α_t)) * sqrt(1 − α_t/α_t−1)
                sigmas_t = ddim_eta * torch.sqrt(
                    (1 - alpha_hat_t_prev) / (1 - alpha_hat_t) * (1 - alpha_hat_t / alpha_hat_t_prev))
            
                # (3) compute "direction pointing to x_{t}"
                pred_dir_xt = torch.sqrt(1 - alpha_hat_t_prev - sigmas_t**2) * predicted_noise

                # (4) random noise
                random_noise = sigmas_t * torch.randn_like(x)
            
                # 4. predict x_{t-1}, formula (12)
                x = torch.sqrt(alpha_hat_t_prev) * pred_x0 + pred_dir_xt + random_noise

        model.train()
        x = (x.clamp(-1, 1) + 1) / 2
        x = (x * 255).type(torch.uint8)
        return x  # [batch, 3, H, W]

def train(args):
    #setup_logging(args.run_name)
    #logger = SummaryWriter(os.path.join("runs", args.run_name, args.dataset))
    logger = get_logger(args.log_dir)
    
    device = args.device
    #dataloader = get_data(args)
    dataset = CustomDataset(args.image_size, args.dataset)
    dataloader = torch.utils.data.DataLoader(dataset,
                                             batch_size=args.batch_size,
                                             shuffle=True,
                                             num_workers=4,
                                             drop_last=False,
                                             pin_memory=True)
    model = UNet()
    if args.pretrained_weight is not None:
        model.load_state_dict(torch.load(args.pretrained_weight,map_location='cpu'))
    model = model.to(device)
    optimizer = optim.AdamW(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones = args.lr_epochs, gamma = 0.1)
    mse = nn.MSELoss()
    diffusion = Diffusion(img_size=args.image_size, device=device)
    
    l = len(dataloader)
    logger.info('\n'
                '========= Training info =========\n'
                'model: {} \n'
                'dataset: {} \n'
                'total training examples: {} \n'
                'image_size: {} \n'
                'start_epoch: {} \n'
                'max_epoch: {} \n'
                'batch_size: {} \n'
                'learning_rate: {} \n'
                'lr epochs: {} \n'
                'pretrained_weight: {} \n'
                '=================================='.format(args.run_name,
                                                            args.dataset,
                                                            len(dataset),
                                                            args.image_size,
                                                            args.start_epoch,
                                                            args.max_epoch,
                                                            args.batch_size,
                                                            args.lr,
                                                            args.lr_epochs,
                                                            args.pretrained_weight))
    best_loss = 999
    for epoch in range(args.start_epoch, args.max_epoch):
        logger.info(f"Starting epoch {epoch}:")
        lr = optimizer.param_groups[0]['lr']
        logger.info(f'Current Learning rate = {lr}')
        #pbar = tqdm(dataloader)
        pbar = dataloader
        mse_loss = AverageMeter()
        for i, images in enumerate(pbar):
            images = images.to(device)
            t = diffusion.sample_timesteps(images.shape[0]).to(device)
            x_t, noise = diffusion.noise_images(images, t)
            predicted_noise = model(x_t, t)
            loss = mse(noise, predicted_noise)
            
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            mse_loss.update(loss.item())
            #pbar.set_postfix(MSE=mse_loss.avg)
            #logger.add_scalar("MSE", loss.item(), global_step=epoch * l + i)
            if i%500 == 0:
                logger.info('Epoch: [{0}][{1}/{2}] '
                            'Loss {loss.val:.4f} ({loss.avg:.4f}) '.format(epoch,
                                                                           i,
                                                                           len(dataloader),
                                                                           loss=mse_loss))

        scheduler.step()
        
        sampled_images = diffusion.sample(model, n=args.num_test_images)
        save_images_cv2(sampled_images, os.path.join("epoch_results", args.run_name, args.dataset, f"{epoch}.jpg"))
        torch.save(model.state_dict(), os.path.join("saved_model", args.run_name, args.dataset, "ckpt.pt"))
        if mse_loss.avg<best_loss:
            best_loss = mse_loss.avg
            torch.save(model.state_dict(), os.path.join("saved_model", args.run_name, args.dataset, "ckpt_best.pt"))


def run_train():
    import argparse
    parser = argparse.ArgumentParser()
    args = parser.parse_args()
    args.run_name = "DDPM_Uncondtional"
    args.start_epoch = 0
    args.max_epoch = 150  # Should be enough for training data with at least 0.1M
    args.batch_size = 4
    args.image_size = 64
    args.dataset = 'church_lsun'
    args.device = "cuda"
    args.lr = 3e-4
    args.lr_epochs = [120]
    args.num_test_images = 3
    args.pretrained_weight = None #'pretrained_model/church_lsun_ckpt.pt'
    if not os.path.exists(os.path.join('saved_model', args.run_name, args.dataset)):
        os.mkdir(os.path.join('saved_model', args.run_name, args.dataset))
    if not os.path.exists(os.path.join('epoch_results', args.run_name, args.dataset)):
        os.mkdir(os.path.join('epoch_results', args.run_name, args.dataset))
    args.log_dir = os.path.join('saved_model', args.run_name, args.dataset)
    train(args)

def generate():
    run_name = "DDPM_Uncondtional"
    dataset = 'church_lsun'
    sample_step = 'ddim' # ddpm or ddim
    image_size = 64
    number_of_generate_example = 9
    device = "cuda"
    save_image_path = 'result.jpg'
    
    model = UNet().to(device)
    ckpt = torch.load(os.path.join('saved_model', run_name, dataset, 'round_1/ckpt_147.pt'))
    model.load_state_dict(ckpt)
    diffusion = Diffusion(img_size=image_size, device=device)
    if sample_step == 'ddpm':
        x = diffusion.sample(model, number_of_generate_example)
    elif sample_step == 'ddim':
        x = diffusion.sample_ddim(model, number_of_generate_example)
    save_images_cv2(x, save_image_path)
    
if __name__ == '__main__':
    # Training and generate are put in one file because I'm too lazy to write another one
    # Run train
    run_train()
    # Run generate
    #generate()
    
