import torch
import torch.nn as nn
from src.misc import apply_norm
from src.conv_tasnet import TemporalConvNet, GlobalLayerNorm, EPS

acts = {
    "hardtanh": nn.Hardtanh,
    "relu": nn.ReLU,
    "leaky_relu": nn.LeakyReLU,
}

class AvgLayer(nn.Module):
    def __init__(self):
        super(AvgLayer, self).__init__()

    def forward(self, x):
        """
        x : B, C, T
        """
        x = x.mean(dim = -1)
        return x

class LSTMClassifer(nn.Module):
    def __init__(self, B, config):
        super(LSTMClassifer, self).__init__()

        self.B = B
        self.num_layers = config['layers']
        self.hidden_size = config['hidden_size']
        self.dropout = config['dropout']

        self.rnns = nn.ModuleList()
        self.norms = nn.ModuleList()
        self.drops = nn.ModuleList()

        for l in range(self.num_layers):

            if l == 0:
                in_dim = self.B
            else:
                in_dim = self.hidden_size * 2

            self.rnns.append(
                    nn.LSTM(in_dim, self.hidden_size, 1, batch_first = True, bidirectional = True))
            self.drops.append(nn.Dropout(self.dropout))
            self.norms.append(nn.LayerNorm(self.hidden_size*2))

        self.out = nn.Linear(self.hidden_size * 2, 1)

    def forward(self, x):

        x = x.permute(0, 2, 1)
        for l in range(self.num_layers):
            x, _ = self.rnns[l](x)
            x = self.norms[l](x)
            x = self.drops[l](x)

        x = x[:, -1, :]
        x = self.out(x)
        return x

class ConvPatchClassifier(nn.Module):
    def __init__(self, B, config):
        super(ConvPatchClassifier, self).__init__()
        self.B = B
        norm_type = config['norm_type']
        layers = config['layers']
        use_layernorm = config.get('layernorm', False)

        in_channel = B
        network = []
        for l, lconf in enumerate(layers):
            out_channel = lconf['filters']
            kernel = lconf['kernel']
            stride = lconf['stride']
            padding = lconf.get('padding', 0)
            layer = nn.Conv1d(in_channel, out_channel,
                    kernel_size = kernel,
                    stride = stride,
                    padding = padding)

            layer = apply_norm(layer, norm_type)
            network.append(layer)

            if use_layernorm and l != len(layers) - 1:
                norm = GlobalLayerNorm(out_channel)
                network.append(norm)

            if l != len(layers) - 1:
                network.append(nn.LeakyReLU(0.1))
            in_channel = out_channel

        self.network = nn.Sequential(*network)
    def forward(self, x):
        x = self.network(x)
        return x

class DomainClassifier(nn.Module):
    def __init__(self, B, config):
        super(DomainClassifier, self).__init__()

        self.B = B
        mtype = config['type']
        self.mtype = mtype

        if mtype == 'conv':
            in_channel = B
            network = []

            for l, lconf in enumerate(layers):
                out_channel = lconf['filters']
                kernel = lconf['kernel']
                stride = lconf['stride']
                padding = 0

                layer = nn.Conv1d(in_channel, out_channel,
                        kernel_size = kernel,
                        stride = stride,
                        padding = padding)

                layer = apply_norm(layer, norm_type)
                network.append(layer)
                if l != len(layers) - 1:
                    network.append(act())
                in_channel = out_channel

            network.append(AvgLayer())
            final = nn.Linear(in_channel, 1)
            network.append(final)
            self.network = nn.Sequential(*network)

        elif mtype == 'conv-patch':
            self.network = ConvPatchClassifier(B, config)

        elif mtype == 'conv2d-patch':
            self.network = Conv2dClassifier(B, config)

        elif mtype == 'LSTM':
            in_channel = B
            self.network = LSTMClassifer(B, config)

        elif mtype == 'linear':
            raise NotImplementedError

        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_normal_(p)

    def forward(self, feature):
        x = self.network(feature)
        return x

class GLN_2D(nn.Module):
    """Global Layer Normalization (gLN)"""
    def __init__(self, channel_size):
        super(GLN_2D, self).__init__()
        self.gamma = nn.Parameter(torch.Tensor(1, channel_size, 1, 1))  # [1, N, 1, 1]
        self.beta = nn.Parameter(torch.Tensor(1, channel_size, 1, 1))  # [1, N, 1, 1]
        self.reset_parameters()

    def reset_parameters(self):
        self.gamma.data.fill_(1)
        self.beta.data.zero_()

    def forward(self, y):
        """
        Args:
            y: [M, Channel, F, T]
        Returns:
            gLN_y: [M, Channel, F, T]
        """
        mean = y.mean(dim=1, keepdim=True).mean(dim=2, keepdim=True).mean(dim=3, keepdim=True) #[M, 1, 1, 1]
        var = (torch.pow(y-mean, 2)).mean(dim=1, keepdim=True).mean(dim=2, keepdim=True).mean(dim=3, keepdim=True)
        gLN_y = self.gamma * (y - mean) / torch.pow(var + EPS, 0.5) + self.beta
        return gLN_y

class CDAN_Dis(nn.Module):
    def __init__(self, B, C, config):
        super(CDAN_Dis, self).__init__()

        self.B = B
        self.C = C
        mtype = config['type']
        self.mtype = mtype

        norm_type = config['norm_type']
        layers = config['layers']
        use_layernorm = config.get('layernorm', False)

        # Define 2D part
        layers_2d = []
        in_channels = B*C

        l1 = apply_norm(nn.Conv2d(in_channels, 1, kernel_size=(1, 1)), norm_type)
        #l2 = apply_norm(nn.Conv2d(B, 1, kernel_size=(1, 1)), norm_type)

        layers_2d.append(l1)
        if use_layernorm:
            layers_2d.append(GLN_2D(1))
        layers_2d.append(nn.LeakyReLU(0.1))
        self.net_2d = nn.Sequential(*layers_2d)

        in_channel = B
        network = []
        for l, lconf in enumerate(layers):
            out_channel = lconf['filters']
            kernel = lconf['kernel']
            stride = lconf['stride']
            padding = lconf.get('padding', 0)
            layer = nn.Conv1d(in_channel, out_channel,
                    kernel_size = kernel,
                    stride = stride,
                    padding = padding)

            layer = apply_norm(layer, norm_type)
            network.append(layer)

            if use_layernorm and l != len(layers) - 1:
                norm = GlobalLayerNorm(out_channel)
                network.append(norm)

            if l != len(layers) - 1:
                network.append(nn.LeakyReLU(0.1))
            in_channel = out_channel

        self.network = nn.Sequential(*network)

        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_normal_(p)

    def forward(self, feature, mask):
        # [ B, CF, F, T ]
        B, F, T = feature.size()
        d_in = torch.einsum('bft,bcpt->bcfpt', feature, mask)
        d_in = d_in.view(B, -1, F, T)

        # [ B, F, T ]
        d_in = self.net_2d(d_in)
        d_in = d_in.squeeze(1)
        x = self.network(d_in)
        return x
