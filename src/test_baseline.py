import os
import time
import yaml
import json
import datetime

import torch

from tqdm import tqdm
from torch.utils.data import DataLoader

from src.solver import Solver
from src.utils import DEV, DEBUG, NCOL, read_scale
from src.conv_tasnet import ConvTasNet
from src.pit_criterion import cal_loss
from src.dataset import wsj0_eval
from src.wham import wham_eval
from src.evaluation import cal_SDR, cal_SISNRi
from src.sep_utils import remove_pad, load_mix_sdr
from src.gender_mapper import GenderMapper

class Tester(Solver):
    def __init__(self, config):
        super(Tester, self).__init__(config)

        self.tr_config = config['solver']['train_config']
        self.tr_config = yaml.load(open(self.tr_config), Loader=yaml.FullLoader)

        self.result_dir = config['solver']['result_dir']
        self.safe_mkdir(self.result_dir)

        self.checkpoint = config['solver']['checkpoint']

        self.batch_size = 1
        self.num_workers = 4

        save_dict = torch.load(self.checkpoint, map_location=torch.device('cpu'))
        self.epoch = save_dict['epoch']
        self.valid_score = save_dict['valid_score']
        self.optim_dict = save_dict['optim']

        state_dict = save_dict['state_dict']
        self.set_model(state_dict)

        self.compute_sdr = config['solver'].get('compute_sdr', False)
        self.g_mapper = GenderMapper()

    def load_dset(self, dset):
        # root: wsj0_root, vctk_root, libri_root
        d = 'wsj' if dset == 'wsj0' else dset # stupid error
        if 'wham' in dset:
            # load wham, wham-easy
            return self.load_wham(dset)

        audio_root = self.config['data'][f'{d}_root']
        cv_list = f'./data/{dset}/id_list/cv.pkl'
        tt_list = f'./data/{dset}/id_list/tt.pkl'

        print(f'Load cv from {cv_list}')
        print(f'Load tt from {tt_list}')

        devset = wsj0_eval(cv_list,
                audio_root = audio_root,
                pre_load = False)
        cv_loader = DataLoader(devset,
                batch_size = self.batch_size,
                shuffle = False,
                num_workers = self.num_workers)

        testset = wsj0_eval(tt_list,
                audio_root = audio_root,
                pre_load = False)
        tt_loader = DataLoader(testset,
                batch_size = self.batch_size,
                shuffle = False,
                num_workers = self.num_workers)
        return cv_loader, tt_loader

    def load_wham(self, dset):
        audio_root = self.config['data'][f'wsj_root']
        cv_list = f'./data/wsj0/id_list/cv.pkl'
        tt_list = f'./data/wsj0/id_list/tt.pkl'

        scale = read_scale(f'./data/{dset}')
        print(f'Load wham data with scale {scale}')

        devset = wham_eval(cv_list,
                audio_root = audio_root,
                pre_load = False,
                mode = 'cv',
                scale = scale)
        cv_loader = DataLoader(devset,
                batch_size = self.batch_size,
                shuffle = False,
                num_workers = self.num_workers)

        testset = wham_eval(tt_list,
                audio_root = audio_root,
                pre_load = False,
                mode = 'tt',
                scale = scale)
        tt_loader = DataLoader(testset,
                batch_size = self.batch_size,
                shuffle = False,
                num_workers = self.num_workers)
        return cv_loader, tt_loader

    def set_model(self, state_dict):
        model_type = self.tr_config['model'].get('type', 'convtasnet')
        if model_type == 'adanet':
            self.model = ADANet(self.tr_config['model']).to(DEV)
        elif model_type == 'slim':
            self.model = MyConvTasNet(self.tr_config['model']).to(DEV)
        else:
            self.model = ConvTasNet(self.tr_config['model']).to(DEV)
        self.model.load_state_dict(state_dict)

    def print_info(self):
        print(f'Epoch: {self.epoch}')

    def exec(self):
        self.print_info()

        self.model.eval()
        dsets = self.config['data']['dsets']

        ds = ', '.join(dsets)
        print(f"Evaluate following datasets: {ds}")

        result_dict = {}

        splts = [ 'cv', 'tt' ]
        gs = [ 'MM', 'FF', 'MF' ]
        sdr_keys = []
        for splt in splts:
            for g in gs:
                sdr_keys.append(f'{splt}_{g}')
        sdr_keys = splts + sdr_keys

        for dset in dsets:
            cv_loader, tt_loader = self.load_dset(dset)
            sdr0 = load_mix_sdr(f'./data/{dset}/mix_sdr/', sdr_keys)

            result_dict[dset] = {}

            r = self.evaluate(cv_loader, 'cv', dset, sdr0)
            result_dict[dset]['cv'] = r

            r = self.evaluate(tt_loader, 'tt', dset, sdr0)
            result_dict[dset]['tt'] = r

        result_dict['tr_config'] = self.tr_config
        rname = os.path.join(self.result_dir, 'result.json')
        json.dump(result_dict, open(rname, 'w'), indent = 1)
        return result_dict

    def evaluate(self, loader, dset, dataset, sdr0):
        total_loss = 0.
        total_SISNRi = 0
        total_SDR = 0
        total_cnt = 0

        gs = [ 'MM', 'FF', 'MF' ]
        gender_SISNRi = { g: 0. for g in gs }
        gender_SDR = { g: 0. for g in gs }
        gender_cnt = { g: 0. for g in gs }

        with torch.no_grad():
            for i, sample in enumerate(tqdm(loader, ncols = NCOL)):

                padded_mixture = sample['mix'].to(DEV)
                padded_source = sample['ref'].to(DEV)
                mixture_lengths = sample['ilens'].to(DEV)
                uids = sample['uid']

                ml = mixture_lengths.max().item()
                padded_mixture = padded_mixture[:, :ml]
                padded_source = padded_source[:, :, :ml]

                estimate_source = self.model(padded_mixture)

                loss, max_snr, estimate_source, reorder_estimate_source = \
                    cal_loss(padded_source, estimate_source, mixture_lengths)

                total_loss += loss.item()

                B = reorder_estimate_source.size(0)
                total_cnt += B

                padded_mixture = remove_pad(padded_mixture, mixture_lengths)
                padded_source = remove_pad(padded_source, mixture_lengths)
                reorder_estimate_source = remove_pad(reorder_estimate_source, mixture_lengths)

                for b in range(B):
                    mix = padded_mixture[b]
                    src_ref = padded_source[b]
                    src_est = reorder_estimate_source[b]
                    uid = uids[b]

                    g = self.g_mapper(uid, dataset)
                    gender_cnt[g] += 1

                    sisnri = cal_SISNRi(src_ref, src_est, mix)
                    total_SISNRi += sisnri
                    gender_SISNRi[g] += sisnri

                    if self.compute_sdr:
                        sdr = cal_SDR(src_ref, src_est)
                        total_SDR += sdr
                        gender_SDR[g] += sdr

        total_loss /= total_cnt
        total_SISNRi /= total_cnt

        if self.compute_sdr:
            total_SDR /= total_cnt
            total_SDRi = total_SDR - sdr0[dset]
        else:
            total_SDRi = 0

        gender_SDRi = {}
        for g in gender_SISNRi:
            gender_SISNRi[g] /= gender_cnt[g]
            if self.compute_sdr:
                sdr = gender_SDR[g] / gender_cnt[g]
                gender_SDRi[g] = sdr - sdr0[f'{dset}_{g}']
            else:
                gender_SDRi[g] = 0.

        result = { 'total_loss': total_loss, 'total_SDRi': total_SDRi, 'total_SISNRi': total_SISNRi,
                   'gender_SDRi': gender_SDRi, 'gender_SISNRi': gender_SISNRi }
        return result

