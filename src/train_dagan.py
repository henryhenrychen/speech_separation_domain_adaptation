import os
import time
import yaml
import math
import random
import datetime
import numpy as np
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
from src.da_conv_tasnet import DAConvTasNet
from src.domain_cls import DomainClassifier
from src.pit_criterion import cal_loss, cal_norm
from src.dataset import wsj0
from src.vctk import VCTK
from src.scheduler import RampScheduler, ConstantScheduler, DANNScheduler
from src.gradient_penalty import calc_gradient_penalty

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
        self.pretrain_d_step = config['solver'].get('pretrain_d_step', 0)

        self.g_iters = config['solver']['g_iters']
        self.d_iters = config['solver']['d_iters']

        self.adv_loss = config['solver']['adv_loss']
        self.gp_lambda = config['solver']['gp_lambda']

        self.load_data()
        self.set_model()

    def load_data(self):
        self.load_wsj0_data()
        self.load_vctk_data()

    def load_wsj0_data(self):

        seg_len = self.config['data']['wsj0']['segment']
        audio_root = self.config['data']['wsj_root']

        trainset = wsj0('./data/wsj0/id_list/tr.pkl',
                audio_root = audio_root,
                seg_len = seg_len,
                pre_load = False,
                one_chunk_in_utt = True,
                mode = 'tr')
        self.wsj0_tr_loader = DataLoader(trainset,
                batch_size = self.batch_size,
                shuffle = True,
                num_workers = self.num_workers,
                drop_last = True)
        self.wsj0_gen = inf_data_gen(self.wsj0_tr_loader)

        devset = wsj0('./data/wsj0/id_list/cv.pkl',
                audio_root = audio_root,
                seg_len = seg_len,
                pre_load = False,
                one_chunk_in_utt = False,
                mode = 'cv')
        self.wsj0_cv_loader = DataLoader(devset,
                batch_size = self.batch_size,
                shuffle = False,
                num_workers = self.num_workers)

    def load_vctk_data(self):

        seg_len = self.config['data']['vctk']['segment']
        audio_root = self.config['data']['vctk_root']

        trainset = VCTK('./data/vctk/id_list/tr.pkl',
                audio_root = audio_root,
                seg_len = seg_len,
                pre_load = False,
                one_chunk_in_utt = True,
                mode = 'tr')
        self.vctk_tr_loader = DataLoader(trainset,
                batch_size = self.batch_size,
                shuffle = True,
                num_workers = self.num_workers,
                drop_last = True)
        self.vctk_gen = inf_data_gen(self.vctk_tr_loader)

        devset = VCTK('./data/vctk/id_list/cv.pkl',
                audio_root = audio_root,
                seg_len = seg_len,
                pre_load = False,
                one_chunk_in_utt = False,
                mode = 'cv')
        self.vctk_cv_loader = DataLoader(devset,
                batch_size = self.batch_size,
                shuffle = False,
                num_workers = self.num_workers)

    def set_optim(self, models, opt_config):

        params = []
        for m in models:
            params += list(m.parameters())

        lr = opt_config['lr']
        weight_decay = opt_config['weight_decay']

        optim_type = opt_config['type']
        if optim_type == 'SGD':
            momentum = opt_config['momentum']
            opt = torch.optim.SGD(
                    params,
                    lr = lr,
                    momentum = momentum,
                    weight_decay = weight_decay)
        elif optim_type == 'Adam':
            opt = torch.optim.Adam(
                    params,
                    lr = lr,
                    weight_decay = weight_decay)
        elif optim_type == 'ranger':
            opt = Ranger(
                    model.params,
                    lr = lr,
                    weight_decay = weight_decay)
        else:
            print('Specify optim')
            exit()
        return opt

    def set_scheduler(self, sch_config):
        if sch_config['function'] == 'ramp':
            return RampScheduler(sch_config['start_step'],
                                 sch_config['end_step'],
                                 sch_config['start_value'],
                                 sch_config['end_value'])
        elif sch_config['function'] == 'constant':
            return ConstantScheduler(sch_config['value'])


    def set_model(self):

        self.G = DAConvTasNet(self.config['model']['gen']).to(DEV)
        self.D = DomainClassifier(self.G.B, self.config['model']['domain_cls']).to(DEV)

        self.g_optim = self.set_optim([self.G], self.config['g_optim'])
        self.d_optim = self.set_optim([self.D], self.config['d_optim'])

        self.bce_loss = nn.BCEWithLogitsLoss()
        self.src_label = torch.FloatTensor([0]).to(DEV)
        self.tgt_label = torch.FloatTensor([1]).to(DEV)

        model_path = self.config['solver']['resume']
        if model_path != '':
            print('Resuming Training')
            print(f'Loading Model: {model_path}')

            info_dict = torch.load(model_path)

            print(f"Previous score: {info_dict['valid_score']}")

            self.G.load_state_dict(info_dict['state_dict'])
            self.D.load_state_dict(info_dict['D_state_dict'])

            print('Loading complete')

            if self.config['solver']['resume_optim']:
                print('Loading optim')

                optim_dict = info_dict['g_optim']
                self.g_optim.load_state_dict(optim_dict)

                optim_dict = info_dict['d_optim']
                self.d_optim.load_state_dict(optim_dict)

        self.Lg_scheduler = self.set_scheduler(self.config['solver']['Lg_scheduler'])
        self.Ld_scheduler = self.set_scheduler(self.config['solver']['Ld_scheduler'])

        self.use_scheduler = False
        if 'scheduler' in self.config['solver']:
            self.use_scheduler = self.config['solver']['scheduler']['use']
            self.scheduler_type = self.config['solver']['scheduler']['type']

            if self.scheduler_type == 'ReduceLROnPlateau':
                patience = self.config['solver']['scheduler']['patience']
                self.lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
                        self.g_optim,
                        mode = 'min',
                        factor = 0.5,
                        patience = patience,
                        verbose = True)

    def log_meta(self, meta, dset):
        for key in meta:
            value = meta[key]
            name = f'{dset}_{key}'
            self.writer.add_scalar(f'valid/{name}', value, self.valid_time)

    def exec(self):

        self.G.train()
        for step in tqdm(range(0, self.pretrain_d_step), ncols = NCOL):
            self.train_dis_once(step, self.wsj0_gen, self.vctk_gen, pretrain = True)

        for step in tqdm(range(self.start_step, self.total_steps), ncols = NCOL):

            # supervised
            self.train_sup_once(step, self.wsj0_gen)

            # semi part
            self.train_dis_once(step, self.wsj0_gen, self.vctk_gen)
            self.train_gen_once(step, self.wsj0_gen, self.vctk_gen)

            if step % self.valid_step == 0 and step != 0:
                self.G.eval()
                wsj0_meta = self.valid(self.wsj0_cv_loader, self.src_label)
                vctk_meta = self.valid(self.vctk_cv_loader, self.tgt_label)
                self.G.train()

                if self.use_scheduler:
                    if self.scheduler_type == 'ReduceLROnPlateau':
                        self.lr_scheduler.step(wsj0_meta['valid_loss'])

                # Do saving
                self.log_meta(wsj0_meta, 'wsj0')
                self.log_meta(vctk_meta, 'vctk')

                model_name = f'{step}.pth'
                valid_score = { 'wsj0': wsj0_meta, 'vctk': vctk_meta }
                info_dict = { 'step': step, 'valid_score': valid_score }
                info_dict['g_optim'] = self.g_optim.state_dict()
                info_dict['d_optim'] = self.d_optim.state_dict()
                info_dict['D_state_dict'] = self.D.state_dict()

                # TODO, use vctk_loss as save crit
                save_crit = vctk_meta['valid_loss']
                self.saver.update(self.G, save_crit, model_name, info_dict)

                model_name = 'latest.pth'
                self.saver.force_save(self.G, model_name, info_dict)

                self.valid_time += 1


    def train_sup_once(self, step, data_gen):

        sample = data_gen.__next__()

        padded_mixture = sample['mix'].to(DEV)
        padded_source = sample['ref'].to(DEV)
        mixture_lengths = sample['ilens'].to(DEV)

        estimate_source, _ = self.G(padded_mixture)

        loss, max_snr, estimate_source, reorder_estimate_source = \
            cal_loss(padded_source, estimate_source, mixture_lengths)

        self.g_optim.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.G.parameters(), self.G_grad_clip)
        self.g_optim.step()

    def train_dis_once(self, step, src_gen, tgt_gen, pretrain = False):
        # assert batch_size is even

        if pretrain:
            prefix = 'pretrain_'
        else:
            prefix = ''

        total_d_loss = 0.
        weighted_d_loss = 0.
        total_gp = 0.
        domain_acc = 0.
        cnt = 0
        for _ in range(self.d_iters):

            # fake(src) sample
            sample = src_gen.__next__()
            src_mixture = sample['mix'].to(DEV)

            with torch.no_grad():
                _, src_feat = self.G(src_mixture)

            if self.adv_loss == 'wgan-gp':
                d_fake_loss = self.D(src_feat).mean()
            elif self.adv_loss == 'gan':
                d_fake_out = self.D(src_feat)
                d_fake_loss = self.bce_loss(d_fake_out,
                                            self.src_label.expand_as(d_fake_out))
                with torch.no_grad():
                    src_dp = ((F.sigmoid(d_fake_out) >= 0.5).float() == self.src_label).float()
                    domain_acc += src_dp.sum().item()
                    cnt += src_dp.numel()
                    self.writer.add_scalar(f'train/{prefix}dis_src_domain_acc', src_dp.mean().item(), step)

            # true(tgt) sample
            sample = tgt_gen.__next__()
            tgt_mixture = sample['mix'].to(DEV)

            with torch.no_grad():
                _, tgt_feat = self.G(tgt_mixture)

            if self.adv_loss == 'wgan-gp':
                d_real_loss = - self.D(tgt_feat).mean()
            elif self.adv_loss == 'gan':
                d_real_out = self.D(tgt_feat)
                d_real_loss = self.bce_loss(d_real_out,
                                            self.tgt_label.expand_as(d_real_out))
                with torch.no_grad():
                    tgt_dp = ((F.sigmoid(d_real_out) >= 0.5).float() == self.tgt_label).float()
                    domain_acc += tgt_dp.sum().item()
                    cnt += tgt_dp.numel()
                    self.writer.add_scalar(f'train/{prefix}dis_tgt_domain_acc', tgt_dp.mean().item(), step)

            d_loss = d_real_loss + d_fake_loss

            if self.adv_loss == 'wgan-gp':
                gp = calc_gradient_penalty(self.D, tgt_feat, src_feat)
                d_lambda = self.Ld_scheduler.value(step)
                d_loss = d_loss + self.gp_lambda * gp
                total_gp += gp.item()

            d_lambda = self.Ld_scheduler.value(step)
            _d_loss = d_lambda * d_loss

            total_d_loss += d_loss.item()
            weighted_d_loss += _d_loss.item()

            self.d_optim.zero_grad()
            _d_loss.backward()
            grad_norm = torch.nn.utils.clip_grad_norm_(self.D.parameters(), self.D_grad_clip)
            if math.isnan(grad_norm):
                print('Error : grad norm is NaN @ step '+str(step))
            else:
                self.d_optim.step()

        total_d_loss /= self.d_iters
        weighted_d_loss /= self.d_iters
        total_gp /= self.d_iters

        self.writer.add_scalar(f'train/{prefix}d_loss', total_d_loss, step)
        if self.adv_loss == 'wgan-gp':
            self.writer.add_scalar(f'train/{prefix}gradient_penalty', total_gp, step)
        elif self.adv_loss == 'gan':
            domain_acc = domain_acc / cnt
            self.writer.add_scalar(f'train/{prefix}dis_domain_acc', domain_acc, step)

    def train_gen_once(self, step, src_gen, tgt_gen):
        # Only remain gan now

        total_g_loss = 0.
        weighted_g_loss = 0.
        domain_acc = 0.
        cnt = 0
        for _ in range(self.g_iters):

            # fake(src) sample
            sample = src_gen.__next__()
            src_mixture = sample['mix'].to(DEV)

            _, src_feat = self.G(src_mixture)

            if self.adv_loss == 'wgan-gp':
                g_fake_loss = - self.D(src_feat).mean()
            elif self.adv_loss == 'gan':
                g_fake_out = self.D(src_feat)
                g_fake_loss = self.bce_loss(g_fake_out,
                                            self.tgt_label.expand_as(g_fake_out))
                with torch.no_grad():
                    src_dp = ((F.sigmoid(g_fake_out) >= 0.5).float() == self.src_label).float()
                    domain_acc += src_dp.sum().item()
                    cnt += src_dp.numel()

            # true(tgt) sample
            sample = tgt_gen.__next__()
            tgt_mixture = sample['mix'].to(DEV)

            _, tgt_feat = self.G(tgt_mixture)

            if self.adv_loss == 'wgan-gp':
                g_real_loss = self.D(tgt_feat).mean()
            elif self.adv_loss == 'gan':
                g_real_out = self.D(tgt_feat)
                g_real_loss = self.bce_loss(g_real_out,
                                            self.src_label.expand_as(g_real_out))
                with torch.no_grad():
                    tgt_dp = ((F.sigmoid(g_real_out) >= 0.5).float() == self.tgt_label).float()
                    domain_acc += tgt_dp.sum().item()
                    cnt += tgt_dp.numel()

            g_loss = g_real_loss + g_fake_loss
            g_lambda = self.Lg_scheduler.value(step)
            _g_loss = g_loss * g_lambda

            self.g_optim.zero_grad()
            _g_loss.backward()
            grad_norm = torch.nn.utils.clip_grad_norm_(self.G.parameters(), self.G_grad_clip)
            if math.isnan(grad_norm):
                print('Error : grad norm is NaN @ step '+str(step))
            else:
                self.g_optim.step()

            total_g_loss += g_loss.item()
            weighted_g_loss += _g_loss.item()

        total_g_loss /= self.g_iters
        weighted_g_loss /= self.g_iters
        self.writer.add_scalar('train/g_loss', total_g_loss, step)
        self.writer.add_scalar('train/weighted_g_loss', weighted_g_loss, step)
        if self.adv_loss == 'gan':
            domain_acc = domain_acc / cnt
            self.writer.add_scalar('train/gen_domain_acc', domain_acc, step)

    def valid(self, loader, label):
        total_loss = 0.
        total_snr = 0.
        domain_acc = 0.
        cnt = 0

        with torch.no_grad():
            for i, sample in enumerate(tqdm(loader, ncols = NCOL)):

                padded_mixture = sample['mix'].to(DEV)
                padded_source = sample['ref'].to(DEV)
                mixture_lengths = sample['ilens'].to(DEV)

                estimate_source, feature = self.G(padded_mixture)

                loss, max_snr, estimate_source, reorder_estimate_source = \
                    cal_loss(padded_source, estimate_source, mixture_lengths)

                if self.adv_loss != 'wgan-gp':
                    dp = (F.sigmoid(self.D(feature)) >= 0.5).float()
                    cnt += dp.numel()

                    acc_num = (dp == label).sum().item()
                    domain_acc += float(acc_num)

                total_loss += loss.item()
                total_snr += max_snr.mean().item()

        total_loss = total_loss / len(loader)
        total_snr = total_snr / len(loader)
        domain_acc = domain_acc / cnt

        meta = {}
        meta['valid_loss'] = total_loss
        meta['valid_snr'] = total_snr
        meta['valid_domain_acc'] = domain_acc

        return meta
