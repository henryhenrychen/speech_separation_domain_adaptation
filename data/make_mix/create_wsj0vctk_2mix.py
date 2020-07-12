
import os
import math
import numpy as np
import librosa
import soundfile as sf
import multiprocessing as mp
import argparse

from tqdm import tqdm
from activlev import asl_meter

def arg_parse():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wsj_root', help='Path containing wsj0/', required = True)
    parser.add_argument('--vctk_root', help='Path containing wav48/', required = True)
    parser.add_argument('--out_dir', help='Path for output dir', required = True)
    args = parser.parse_args()
    return args

def save_mkdir(path):
    if not os.path.exists(path):
        os.makedirs(path)

def read_list(path):
    ret = []
    with open(path) as f:
        for line in f:
            line = line.rstrip()

            s1, snr1, s2, snr2 = line.split()
            snr1, snr2 = float(snr1), float(snr2)

            ret.append((s1, snr1, s2, snr2))
    return ret

def norm_audio(audio, sr):

    asl = asl_meter(audio, sr)
    audio_norm = audio / math.sqrt(asl)
    return audio_norm

mix_lists = [ ('./wsj0vctk_info/wsj0vctk_mix_2_spk_cv.txt', 'cv'),
              ('./wsj0vctk_info/wsj0vctk_mix_2_spk_tt.txt', 'tt'),
              ('./wsj0vctk_info/wsj0vctk_mix_2_spk_tr.txt', 'tr'), ]

# ===================
args = parse_args()

# Dir contain wsj0/
#wsj0_root = '/home/riviera1020/Big/Corpus/wsj0-clean-wav/'
wsj0_root = args.wsj_root

# Dir contain wav48/
#vctk_root = '/home/riviera1020/Big/Corpus/VCTK-Corpus/'
vctk_root = args.vctk_root

out_dir = args.out_dir
downsample_rate = 8000
min_max = 'min'
num_workers = 6
# ===================

if __name__ == '__main__':
    save_mkdir(out_dir)

    out_dir = os.path.join(out_dir, min_max)
    save_mkdir(out_dir)

    for mix_list, mode in mix_lists:

        dset_dir = os.path.join(out_dir, mode)
        s1_dir = os.path.join(dset_dir, 's1')
        s2_dir = os.path.join(dset_dir, 's2')
        mix_dir = os.path.join(dset_dir, 'mix')
        save_mkdir(s1_dir)
        save_mkdir(s2_dir)
        save_mkdir(mix_dir)

        mix_list = read_list(mix_list)

        #def _main(idx):
        for idx in tqdm(range(len(mix_list))):
            s1, snr1, s2, snr2 = mix_list[idx]

            name1 = s1.split('/')[-1].split('.')[0]
            name2 = s2.split('/')[-1].split('.')[0]

            r1 = vctk_root if name1[0] == 'p' else wsj0_root
            r2 = vctk_root if name2[0] == 'p' else wsj0_root
            s1 = os.path.join(r1, s1)
            s2 = os.path.join(r2, s2)
            s1, sr1 = sf.read(s1)
            s2, sr2 = sf.read(s2)

            name = f'{name1}_{snr1}_{name2}_{snr2}.wav'
            s1_out_path = os.path.join(s1_dir, name)
            s2_out_path = os.path.join(s2_dir, name)
            mix_out_path = os.path.join(mix_dir, name)
            if os.path.isfile(s1_out_path) and os.path.isfile(s2_out_path) and os.path.isfile(mix_out_path):
                continue

            if downsample_rate != None:
                s1 = librosa.core.resample(s1, sr1, downsample_rate)
                s2 = librosa.core.resample(s2, sr2, downsample_rate)

                sr = downsample_rate

            s1 = norm_audio(s1, sr)
            s2 = norm_audio(s2, sr)

            w1 = 10 ** (snr1/20)
            w2 = 10 ** (snr2/20)

            s1 = w1 * s1
            s2 = w1 * s2

            T1 = s1.shape[0]
            T2 = s2.shape[0]
            if min_max == 'max':
                if T1 < T2:
                    s1 = np.concatenate((s1, np.zeros(T2-T1)))
                elif T1 > T2:
                    s2 = np.concatenate((s2, np.zeros(T1-T2)))
            else:
                if T1 < T2:
                    s2 = s2[:T1]
                elif T1 > T2:
                    s1 = s1[:T2]

            mix = s1 + s2
            max_amp = np.max(np.concatenate((np.abs(s1), np.abs(s2), np.abs(mix))))
            mix_scaling = 1 / max_amp * 0.9

            s1 = s1 * mix_scaling
            s2 = s2 * mix_scaling
            mix = mix * mix_scaling

            name = f'{name1}_{snr1}_{name2}_{snr2}.wav'

            s1_out_path = os.path.join(s1_dir, name)
            sf.write(s1_out_path, s1, sr)

            s2_out_path = os.path.join(s2_dir, name)
            sf.write(s2_out_path, s2, sr)

            mix_out_path = os.path.join(mix_dir, name)
            sf.write(mix_out_path, mix, sr)

