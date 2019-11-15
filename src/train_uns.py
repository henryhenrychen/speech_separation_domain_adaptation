import os
import time
import yaml
import math
import datetime

import torch
import torch.nn as nn
import torch.nn.functional as F

from tqdm import tqdm
from tensorboardX import SummaryWriter
from torch.utils.data import DataLoader

from src.solver import Solver
from src.saver import Saver
from src.utils import DEV, DEBUG, NCOL, inf_data_gen
from src.conv_tasnet import ConvTasNet
from src.pit_criterion import cal_loss, cal_norm
from src.dataset import wsj0
from src.ranger import Ranger
from src.discriminator import RWD

class Trainer(Solver):

    def __init__(self, config, stream = None):
        #def __init__(self, data, model, optimizer, args):
        super(Trainer, self).__init__(config)

        self.exp_name = config['solver']['exp_name']

        ts = time.time()
        st = datetime.datetime.fromtimestamp(ts).strftime('%Y_%m_%d_%H_%M_%S')

        save_name = self.exp_name + '-' + st
        self.save_dir = os.path.join(config['solver']['save_dir'], save_name)
        self.safe_mkdir(self.save_dir)
        self.saver = Saver(config['solver']['max_save_num'], self.save_dir, 'min')
        yaml.dump(config, open(os.path.join(self.save_dir, 'config.yaml'), 'w'),
                default_flow_style = False ,indent = 4)

        log_name = self.exp_name + '-' + st
        self.log_dir = os.path.join(config['solver']['log_dir'], log_name)
        self.safe_mkdir(self.log_dir)
        self.writer = SummaryWriter(self.log_dir)

        if stream != None:
            self.writer.add_text('Config', stream)

        self.total_steps = config['solver']['total_steps']
        self.start_step = config['solver']['start_step']
        self.batch_size = config['solver']['batch_size']
        self.D_grad_clip = config['solver']['D_grad_clip']
        self.G_grad_clip = config['solver']['G_grad_clip']
        self.num_workers = config['solver']['num_workers']
        self.valid_step = config['solver']['valid_step']
        self.valid_time = 0

        self.g_iters = config['solver']['g_iters']
        self.d_iters = config['solver']['d_iters']
        self.Lc_lambda = config['solver']['Lc_lambda']
        self.Le_lambda = config['solver']['Le_lambda']

        self.load_data()
        self.set_model()

    def load_data(self):

        seg_len = self.config['data']['segment']
        audio_root = self.config['data']['wsj_root']

        trainset = wsj0('./data/wsj0/id_list/tr.pkl',
                audio_root = audio_root,
                seg_len = seg_len,
                pre_load = False,
                one_chunk_in_utt = True,
                mode = 'tr')
        self.tr_loader = DataLoader(trainset,
                batch_size = self.batch_size,
                shuffle = True,
                num_workers = self.num_workers,
                drop_last = True)
        self.data_gen = inf_data_gen(self.tr_loader)

        devset = wsj0('./data/wsj0/id_list/cv.pkl',
                audio_root = audio_root,
                seg_len = seg_len,
                pre_load = False,
                one_chunk_in_utt = False,
                mode = 'cv')
        self.cv_loader = DataLoader(devset,
                batch_size = self.batch_size,
                shuffle = False,
                num_workers = self.num_workers)

    def set_optim(self, model, opt_config):
        lr = opt_config['lr']
        weight_decay = opt_config['weight_decay']

        optim_type = opt_config['type']
        if optim_type == 'SGD':
            momentum = opt_config['momentum']
            opt = torch.optim.SGD(
                    model.parameters(),
                    lr = lr,
                    momentum = momentum,
                    weight_decay = weight_decay)
        elif optim_type == 'Adam':
            opt = torch.optim.Adam(
                    model.parameters(),
                    lr = lr,
                    weight_decay = weight_decay)
        elif optim_type == 'ranger':
            opt = Ranger(
                    model.parameters(),
                    lr = lr,
                    weight_decay = weight_decay)
        else:
            print('Specify optim')
            exit()
        return opt

    def set_model(self):
        self.G = ConvTasNet(self.config['model']['gen']).to(DEV)
        self.D = RWD(self.config['model']['dis']).to(DEV)

        self.real_label = torch.ones((1,)).to(DEV)
        self.fake_label = torch.zeros((1,)).to(DEV)

        self.g_optim = self.set_optim(self.G, self.config['g_optim'])
        self.d_optim = self.set_optim(self.D, self.config['d_optim'])

        model_path = self.config['solver']['resume']
        if model_path != '':
            print('Resuming Training')
            print(f'Loading Model: {model_path}')

            info_dict = torch.load(model_path)

            print(f"Previous score: {info_dict['valid_score']}")
            self.start_epoch = info_dict['epoch'] + 1

            self.G.load_state_dict(info_dict['state_dict'])
            self.D.load_state_dict(info_dict['D_state_dict'])
            print('Loading complete')

            if self.config['solver']['resume_optim']:
                print('Loading optim')

                optim_dict = info_dict['g_optim']
                self.g_optim.load_state_dict(optim_dict)

                optim_dict = info_dict['d_optim']
                self.d_optim.load_state_dict(optim_dict)

        # TODO, diff scheduler for G and D
        self.use_scheduler = False
        '''
        if 'scheduler' in self.config['solver']:
            self.use_scheduler = self.config['solver']['scheduler']['use']
            self.scheduler_type = self.config['solver']['scheduler']['type']

            if self.scheduler_type == 'ReduceLROnPlateau':
                patience = self.config['solver']['scheduler']['patience']
                self.lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
                        self.opt,
                        mode = 'min',
                        factor = 0.5,
                        patience = patience,
                        verbose = True)
        '''

    def exec(self):

        self.G.train()
        for step in tqdm(range(self.start_step, self.total_steps), ncols = NCOL):

            self.train_dis_once(step)
            self.train_gen_once(step)

            if step % self.valid_step == 0 and step != 0:
                self.G.eval()
                self.valid(self.cv_loader, step)
                self.G.train()

    def train_dis_once(self, step):
        # assert batch_size is even

        total_d_loss = 0.
        for _ in range(self.d_iters):

            # fake sample
            sample = self.data_gen.__next__()
            padded_mixture = sample['mix'].to(DEV)

            estimate_source = self.G(padded_mixture)

            with torch.no_grad():
                y1, y2 = torch.chunk(estimate_source, 2, dim = 0)
                remix = y1 + y2
                remix = remix.view(-1, remix.size(-1))

            d_fake_loss = 0.
            d_fakes = self.D(remix)
            for d_fake in d_fakes:
                d_fake_loss += F.relu(1.0 + d_fake).mean()

            # true sample
            sample = self.data_gen.__next__()
            padded_mixture = sample['mix'].to(DEV)

            d_real_loss = 0.
            d_reals = self.D(padded_mixture)
            for d_real in d_reals:
                d_real_loss += F.relu(1.0 - d_real).mean()

            d_loss = d_real_loss + d_fake_loss

            self.d_optim.zero_grad()
            d_loss.backward()
            grad_norm = torch.nn.utils.clip_grad_norm_(self.D.parameters(), self.D_grad_clip)
            if math.isnan(grad_norm):
                print('Error : grad norm is NaN @ step '+str(step))
            else:
                self.d_optim.step()

            total_d_loss += d_loss.item()

        total_d_loss /= self.d_iters
        self.writer.add_scalar('train/d_loss', total_d_loss, step)

    def train_gen_once(self, step):

        total_g_loss = 0.
        total_gan_loss = 0.
        total_Le = 0.
        total_Lc = 0.
        for _ in range(self.g_iters):

            sample = self.data_gen.__next__()
            padded_mixture = sample['mix'].to(DEV)
            T = padded_mixture.size(-1)

            estimate_source = self.G(padded_mixture)
            y1, y2 = torch.chunk(estimate_source, 2, dim = 0)
            remix = (y1 + y2).view(-1, T)
            g_fakes = self.D(remix)

            g_gan_loss = 0.
            for g_fake in g_fakes:
                g_gan_loss += (- g_fake.mean())

            # TODO, better Le?
            Le = (padded_mixture.unsqueeze(1) * estimate_source).sum(dim = -1)
            Le = (Le ** 2).sum(dim = -1).mean()

            Lc = 0.
            if self.Lc_lambda > 0:
                estimate_source = self.G(remix)
                y1, y2 = torch.chunk(estimate_source, 2, dim = 0)

                reremix1 = (y1 + y2).view(-1, T)
                reremix2 = (y1 + y2.flip(dims = [1])).view(-1, T)

                Lc = cal_norm(padded_mixture, reremix1, reremix2)

            g_loss = g_gan_loss + self.Le_lambda * Le + self.Lc_lambda * Lc

            self.g_optim.zero_grad()
            g_loss.backward()
            grad_norm = torch.nn.utils.clip_grad_norm_(self.G.parameters(), self.G_grad_clip)
            if math.isnan(grad_norm):
                print('Error : grad norm is NaN @ step '+str(step))
            else:
                self.g_optim.step()

            total_g_loss += g_loss.item()
            total_gan_loss += g_gan_loss.item()
            total_Le += Le.item()
            total_Lc += Lc.item()

        self.writer.add_scalar('train/total_g_loss', total_g_loss, step)
        self.writer.add_scalar('train/g_loss', total_gan_loss, step)
        self.writer.add_scalar('train/Le_loss', total_Le, step)
        self.writer.add_scalar('train/Lc_loss', total_Lc, step)

    def valid(self, loader, step):
        total_loss = 0.

        with torch.no_grad():
            for i, sample in enumerate(tqdm(loader, ncols = NCOL)):

                padded_mixture = sample['mix'].to(DEV)
                padded_source = sample['ref'].to(DEV)
                mixture_lengths = sample['ilens'].to(DEV)

                estimate_source = self.G(padded_mixture)

                loss, max_snr, estimate_source, reorder_estimate_source = \
                    cal_loss(padded_source, estimate_source, mixture_lengths)

                total_loss += loss.item()

        total_loss = total_loss / len(self.tr_loader)
        self.writer.add_scalar('valid/pit_loss', total_loss, self.valid_time)

        valid_score = {}
        valid_score['valid_loss'] = total_loss

        model_name = f'{step}.pth'
        info_dict = { 'step': step, 'valid_score': valid_score, 'config': self.config }
        info_dict['g_optim'] = self.g_optim.state_dict()
        info_dict['d_optim'] = self.d_optim.state_dict()
        info_dict['D_state_dict'] = self.D.state_dict()

        self.saver.update(self.G, total_loss, model_name, info_dict)

        model_name = 'latest.pth'
        self.saver.force_save(self.G, model_name, info_dict)

        if self.use_scheduler:
            if self.scheduler_type == 'ReduceLROnPlateau':
                self.lr_scheduler.step(total_loss)
            #elif self.scheduler_type in [ 'FlatCosine', 'CosineWarmup' ]:
            #    self.lr_scheduler.step(epoch)

        self.valid_time += 1