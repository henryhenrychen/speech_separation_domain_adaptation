
import os
import time
import yaml
import datetime
import _pickle as cPickle
import soundfile as sf

import torch

from tqdm import tqdm
from torch.utils.data import DataLoader

from src.solver import Solver
from src.saver import Saver
from src.utils import DEV, DEBUG, NCOL, read_scale
from src.conv_tasnet import ConvTasNet
from src.cmvn_tasnet import CMVNConvTasNet
from src.pit_criterion import cal_loss, SISNR
from src.dataset import wsj0, wsj0_eval
from src.wham import wham, wham_eval
from src.gender_dset import wsj0_gender
from src.ranger import Ranger
from src.evaluation import cal_SDR, cal_SISNRi, cal_SISNR
from src.sep_utils import remove_pad, load_mix_sdr
from src.dashboard import Dashboard
from src.gender_mapper import GenderMapper

"""
from src.scheduler import FlatCosineLR, CosineWarmupLR
"""

class Trainer(Solver):

    def __init__(self, config):
        #def __init__(self, data, model, optimizer, args):
        super(Trainer, self).__init__(config)

        self.exp_name = config['solver']['exp_name']

        ts = time.time()
        st = datetime.datetime.fromtimestamp(ts).strftime('%Y_%m_%d_%H_%M_%S')

        self.resume_model = False
        resume_exp_name = config['solver'].get('resume_exp_name', '')
        if resume_exp_name:
            self.resume_model = True
            exp_name = resume_exp_name
            self.save_dir = os.path.join(self.config['solver']['save_dir'], exp_name)
            self.log_dir = os.path.join(self.config['solver']['log_dir'], exp_name)

            if not os.path.isdir(self.save_dir) or not os.path.isdir(self.log_dir):
                print('Resume Exp name Error')
                exit()

            self.saver = Saver(
                    self.config['solver']['max_save_num'],
                    self.save_dir,
                    'max',
                    resume = True,
                    resume_score_fn = lambda x: x['valid_score']['valid_sisnri'])

            self.writer = Dashboard(exp_name, self.config, self.log_dir, resume=True)

        else:
            save_name = self.exp_name + '-' + st
            self.save_dir = os.path.join(config['solver']['save_dir'], save_name)
            self.safe_mkdir(self.save_dir)
            self.saver = Saver(config['solver']['max_save_num'], self.save_dir, 'max')
            yaml.dump(config, open(os.path.join(self.save_dir, 'config.yaml'), 'w'),
                    default_flow_style = False ,indent = 4)

            log_name = self.exp_name + '-' + st
            self.log_dir = os.path.join(config['solver']['log_dir'], log_name)
            self.safe_mkdir(self.log_dir)
            self.writer = Dashboard(log_name, config, self.log_dir)

        self.epochs = config['solver']['epochs']
        self.start_epoch = config['solver']['start_epoch']
        self.batch_size = config['solver']['batch_size']
        self.grad_clip = config['solver']['grad_clip']
        self.num_workers = config['solver']['num_workers']
        self.save_freq = config['solver'].get('save_freq', -1)

        self.step = 0
        self.valid_times = 0

        self.gender = config['data'].get('gender', 'all')
        self.gender_mapper = GenderMapper()
        self.load_data()
        self.set_model()

        self.script_name = os.path.basename(__file__).split('.')[0].split('_')[-1]
        self.writer.add_tag(self.script_name)

    def load_data(self):
        # Set training dataset
        dset = self.config['data'].get('dset', 'wsj0')
        self.dset = dset

        self.dsets = {}
        for d in [ self.dset ]:
            tr_list, tr_loader, cv_loader = self.load_dset(d)
            self.dsets[d] = { 'tr_list': tr_list, 'tr': tr_loader, 'cv': cv_loader }

    def load_dset(self, dset):
        seg_len = self.config['data']['segment']

        # root: wsj0_root, vctk_root, libri_root
        d = 'wsj' if dset == 'wsj0' else dset # stupid error
        if 'wham' in dset:
            return self.load_wham(dset)

        audio_root = self.config['data'][f'{d}_root']
        tr_list = f'./data/{dset}/id_list/tr.pkl'
        cv_list = f'./data/{dset}/id_list/cv.pkl'

        sp_factors = self.config['solver'].get('sp_factors', None)

        if dset == 'wsj0' and self.gender != 'all':
            trainset = wsj0_gender(tr_list,
                    audio_root = audio_root,
                    seg_len = seg_len,
                    pre_load = False,
                    one_chunk_in_utt = True,
                    mode = 'tr',
                    gender = self.gender)
        else:
            trainset = wsj0(tr_list,
                    audio_root = audio_root,
                    seg_len = seg_len,
                    pre_load = False,
                    one_chunk_in_utt = True,
                    mode = 'tr',
                    sp_factors = sp_factors)
        tr_loader = DataLoader(trainset,
                batch_size = self.batch_size,
                shuffle = True,
                num_workers = self.num_workers)

        devset = wsj0_eval(cv_list,
                audio_root = audio_root,
                pre_load = False)
        cv_loader = DataLoader(devset,
                batch_size = self.batch_size,
                shuffle = False,
                num_workers = self.num_workers)
        return tr_list, tr_loader, cv_loader

    def load_wham(self, dset):
        audio_root = self.config['data'][f'wsj_root']
        seg_len = self.config['data']['segment']
        tr_list = f'./data/wsj0/id_list/tr.pkl'
        cv_list = f'./data/wsj0/id_list/cv.pkl'

        scale = read_scale(f'./data/{dset}')
        print(f'Load wham data with scale {scale}')

        trainset = wham(tr_list,
                audio_root = audio_root,
                seg_len = seg_len,
                pre_load = False,
                one_chunk_in_utt = True,
                mode = 'tr',
                scale = scale)
        tr_loader = DataLoader(trainset,
                batch_size = self.batch_size,
                shuffle = True,
                num_workers = self.num_workers)

        devset = wham_eval(cv_list,
                audio_root = audio_root,
                pre_load = False,
                mode = 'cv',
                scale = scale)
        cv_loader = DataLoader(devset,
                batch_size = self.batch_size,
                shuffle = False,
                num_workers = self.num_workers)
        return tr_list, tr_loader, cv_loader

    def set_model(self):

        self.model = CMVNConvTasNet(self.config['model']).to(DEV)

        pretrained = self.config['solver']['pretrained']
        if pretrained != '':
            info_dict = torch.load(pretrained)
            m, u = self.model.load_state_dict(info_dict['state_dict'], strict = False)
            print('Should missing norm buffer')
            print(m)
            print(u)

            print('Load pretrained model')
            if 'epoch' in info_dict:
                print(f"Epochs: {info_dict['epoch']}")
            elif 'step' in info_dict:
                print(f"Steps : {info_dict['step']}")
            print(info_dict['valid_score'])
        else:
            print('Specify pretrained model')
            exit()

        optim_dict = None
        if self.resume_model:
            model_path = os.path.join(self.save_dir, 'latest.pth')
            print('Resuming Training')
            print(f'Loading Model: {model_path}')

            info_dict = torch.load(model_path)

            print(f"Previous score: {info_dict['valid_score']}")
            self.start_epoch = info_dict['epoch'] + 1
            if 'step' in info_dict:
                self.step = info_dict['step']

            self.model.load_state_dict(info_dict['state_dict'])
            print('Loading complete')

            if self.config['solver']['resume_optim']:
                optim_dict = info_dict['optim']

            # dashboard is one-base
            self.writer.set_epoch(self.start_epoch + 1)
            self.writer.set_step(self.step + 1)

            print(self.start_epoch)
            print(self.step)

        lr = self.config['optim']['lr']
        weight_decay = self.config['optim']['weight_decay']

        optim_type = self.config['optim']['type']
        if optim_type == 'SGD':
            momentum = self.config['optim']['momentum']
            self.opt = torch.optim.SGD(
                    self.model.parameters(),
                    lr = lr,
                    momentum = momentum,
                    weight_decay = weight_decay)
        elif optim_type == 'Adam':
            self.opt = torch.optim.Adam(
                    self.model.parameters(),
                    lr = lr,
                    weight_decay = weight_decay)
        elif optim_type == 'ranger':
            self.opt = Ranger(
                    self.model.parameters(),
                    lr = lr,
                    weight_decay = weight_decay)
        else:
            print('Specify optim')
            exit()

        if optim_dict != None:
            print('Resume optim')
            self.opt.load_state_dict(optim_dict)

        self.use_scheduler = False
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

        # Freeze enc & dec
        def freeze_module(m):
            for param in m.parameters():
                param.requires_grad = False

        print('Freeze model')
        freeze_module(self.model.encoder)
        freeze_module(self.model.decoder)

    def exec(self):
        print('Stat Norm Stat')
        self.comp_norm_stat()

        for epoch in tqdm(range(self.start_epoch, self.epochs), ncols = NCOL):

            self.train_one_epoch(epoch, self.dsets[self.dset]['tr'])

            # Valid training dataset
            if self.save_freq > 0 and (epoch + 1) % self.save_freq == 0:
                force_save = True
            else:
                force_save = False
            self.valid(self.dsets[self.dset]['cv'], self.dset, epoch, prefix = self.dset, force_save = force_save)

            # Valid not training dataset
            for dset in self.dsets:
                if dset != self.dset:
                    self.valid(self.dsets[dset]['cv'], dset, epoch, no_save = True, prefix = dset)

            self.writer.epoch()

        """
        if self.test_after_finished:
            conf = self.construct_test_conf(dsets = 'all', sdir = 'chapter3', choose_best = False, compute_sdr = False)
            result = self.run_tester('test_baseline.py', conf)
            result['tt_config'] = conf
            self.writer.log_result(result)
        """

    def comp_norm_stat(self):
        mean = []
        var = []

        d = 'wsj' if self.dset == 'wsj0' else self.dset
        root = self.config['data'][f'{d}_root']

        tr_list = cPickle.load(open(self.dsets[self.dset]['tr_list'], 'rb'))
        with torch.no_grad():
            for uid in tqdm(tr_list, ncols = NCOL):
                mpath = tr_list[uid]['mix'][0]
                mpath = os.path.join(root, mpath)

                audio, sr = sf.read(mpath)
                audio = torch.FloatTensor(audio).to(DEV).unsqueeze(0)

                spec = self.model.encoder(audio)

                spec = spec.squeeze(0)
                T = spec.size(-1)
                m = spec.mean(dim = -1).cpu()

                mean.append((m, T))

            mm, ss = zip(*mean)
            mm = torch.stack(mm, dim = 0)
            ss = torch.FloatTensor(ss)
            m = (mm * ss.unsqueeze(-1)).sum(dim = 0) / ss.sum()
            m = m.to(DEV)

            for uid in tqdm(tr_list, ncols = NCOL):
                mpath = tr_list[uid]['mix'][0]
                mpath = os.path.join(root, mpath)

                audio, sr = sf.read(mpath)
                audio = torch.FloatTensor(audio).to(DEV).unsqueeze(0)

                spec = self.model.encoder(audio)
                spec = spec.squeeze(0)
                T = spec.size(-1)

                v = ((spec - m.unsqueeze(-1))**2).mean(dim = -1)
                v = v.cpu()
                var.append((v, T))

            vv, ss = zip(*var)
            vv = torch.stack(vv, dim = 0)
            ss = torch.FloatTensor(ss)
            v = (vv * ss.unsqueeze(-1)).sum(dim = 0) / ss.sum()

            v = v.to(DEV)

        self.model.set_mv(m, v)
        self.model = self.model.to(DEV)

    def train_one_epoch(self, epoch, tr_loader):
        self.model.train()
        total_loss = 0.
        total_sisnri = 0.
        cnt = 0

        for i, sample in enumerate(tqdm(tr_loader, ncols = NCOL)):

            padded_mixture = sample['mix'].to(DEV)
            padded_source = sample['ref'].to(DEV)
            mixture_lengths = sample['ilens'].to(DEV)

            estimate_source = self.model(padded_mixture)

            loss, max_snr, estimate_source, reorder_estimate_source = \
                cal_loss(padded_source, estimate_source, mixture_lengths)

            self.opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.grad_clip)
            self.opt.step()

            B = padded_source.size(0)
            total_loss += loss.item() * B
            cnt += B
            with torch.no_grad():
                mix_sisnr = SISNR(padded_source, padded_mixture, mixture_lengths)
                total_sisnri += (max_snr - mix_sisnr).sum()

            meta = { 'iter_loss': loss.item() }
            self.writer.log_step_info('train', meta)

            self.step += 1
            self.writer.step()

        total_loss = total_loss / cnt
        total_sisnri = total_sisnri / cnt

        meta = { 'epoch_loss': total_loss,
                 'epoch_sisnri': total_sisnri }
        self.writer.log_epoch_info('train', meta)

    def valid(self, loader, dset, epoch, no_save = False, prefix = "", force_save = False):
        self.model.eval()
        total_loss = 0.
        total_sisnri = 0.
        cnt = 0

        genders = [ 'MF', 'MM', 'FF' ]
        gender_sisnri = { 'MF': 0., 'FF': 0., 'MM': 0, }
        gender_cnt = { 'MF': 0., 'FF': 0., 'MM': 0, }

        with torch.no_grad():
            for i, sample in enumerate(tqdm(loader, ncols = NCOL)):

                padded_mixture = sample['mix'].to(DEV)
                padded_source = sample['ref'].to(DEV)
                mixture_lengths = sample['ilens'].to(DEV)
                uids = sample['uid']

                ml = mixture_lengths.max().item()
                padded_mixture = padded_mixture[:, :ml]
                padded_source = padded_source[:, :, :ml]
                B = padded_source.size(0)

                estimate_source = self.model(padded_mixture)

                loss, max_snr, estimate_source, reorder_estimate_source = \
                    cal_loss(padded_source, estimate_source, mixture_lengths)

                mix_sisnr = SISNR(padded_source, padded_mixture, mixture_lengths)
                max_sisnri = (max_snr - mix_sisnr)

                total_loss += loss.item() * B
                total_sisnri += max_sisnri.sum().item()
                cnt += B

                for b in range(B):
                    g = self.gender_mapper(uids[b], dset)
                    gender_sisnri[g] += max_sisnri[b].item()
                    gender_cnt[g] += 1

        total_sisnri = total_sisnri / cnt
        total_loss = total_loss / cnt

        meta = { f'{prefix}_epoch_loss': total_loss,
                 f'{prefix}_epoch_sisnri': total_sisnri }

        for g in genders:
            gs = gender_sisnri[g] / gender_cnt[g]
            meta[f'{prefix}_epoch_{g}_sisnri'] = gs

        self.writer.log_epoch_info('valid', meta)

        valid_score = {}
        valid_score['valid_loss'] = total_loss
        valid_score['valid_sisnri'] = total_sisnri

        if no_save:
            return

        model_name = f'{epoch}.pth'
        info_dict = { 'epoch': epoch, 'valid_score': valid_score, 'config': self.config }
        info_dict['optim'] = self.opt.state_dict()

        self.saver.update(self.model, total_sisnri, model_name, info_dict)

        if force_save:
            model_name = f'{epoch}_force.pth'
            self.saver.force_save(self.model, model_name, info_dict)

        model_name = 'latest.pth'
        self.saver.force_save(self.model, model_name, info_dict)

        if self.use_scheduler:
            if self.scheduler_type == 'ReduceLROnPlateau':
                self.lr_scheduler.step(total_loss)
            #elif self.scheduler_type in [ 'FlatCosine', 'CosineWarmup' ]:
            #    self.lr_scheduler.step(epoch)