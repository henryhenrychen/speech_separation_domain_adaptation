import yaml
import torch
import _pickle as cPickle

DEV = torch.device('cpu')
DEBUG = False

NCOL = 100

def set_device(use_cuda):
    global DEV
    use_cuda = use_cuda and torch.cuda.is_available()
    DEV = torch.device("cuda" if use_cuda else "cpu")

def set_debug(is_debug):
    global DEBUG
    DEBUG = is_debug

def read_config(path, local_path):

    config = yaml.load(open(path))
    path_conf = yaml.load(open(local_path))

    for key in path_conf:
        path = path_conf[key]
        if 'data' not in config:
            config['data'] = {}
        config['data'][key] = path
    return config

def inf_data_gen(loader):
    while True:
        for s in loader:
            yield s
