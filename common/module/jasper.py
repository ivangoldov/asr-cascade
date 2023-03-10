import torch
import torch.nn as nn

jasper_activations = {
    "hardtanh": nn.Hardtanh,
    "relu": nn.ReLU,
    "selu": nn.SELU,
}


def init_weights(m, mode='xavier_uniform'):
    if isinstance(m, MaskedConv1d):
        init_weights(m.conv, mode)
    if isinstance(m, nn.Conv1d):
        if mode == 'xavier_uniform':
            nn.init.xavier_uniform_(m.weight, gain=1.0)
        elif mode == 'xavier_normal':
            nn.init.xavier_normal_(m.weight, gain=1.0)
        elif mode == 'kaiming_uniform':
            nn.init.kaiming_uniform_(m.weight, nonlinearity="relu")
        elif mode == 'kaiming_normal':
            nn.init.kaiming_normal_(m.weight, nonlinearity="relu")
        else:
            raise ValueError("Unknown Initialization mode: {0}".format(mode))
    elif isinstance(m, nn.BatchNorm1d):
        if m.track_running_stats:
            m.running_mean.zero_()
            m.running_var.fill_(1)
            m.num_batches_tracked.zero_()
        if m.affine:
            nn.init.ones_(m.weight)
            nn.init.zeros_(m.bias)


def get_same_padding(kernel_size, stride, dilation):
    if stride > 1 and dilation > 1:
        raise ValueError("Only stride OR dilation may be greater than 1")
    if dilation > 1:
        return (dilation * kernel_size) // 2 - 1
    return kernel_size // 2


class JasperEncoder(nn.Module):
    """Jasper encoder
    """

    def __init__(self, **kwargs):
        config = {}
        for key, value in kwargs.items():
            config[key] = value

        nn.Module.__init__(self)
        self._config = config

        activation = jasper_activations[config['activation']]()
        normalization_mode = config.get('normalization_mode', "batch")
        residual_mode = config.get("residual_mode", "add")
        norm_groups = config.get("norm_groups", -1)
        self.use_conv_mask = config.get('convmask', False)
        input_features = config['input_dim']
        self._input_dim = input_features
        init_mode = config.get('init_mode', 'xavier_uniform')

        residual_panes = []
        encoder_layers = []
        self.dense_residual = False
        for layer_config in config['model_definition']:
            dense_res = []
            if layer_config.get('residual_dense', False):
                residual_panes.append(input_features)
                dense_res = residual_panes
                self.dense_residual = True
            groups = layer_config.get('groups', 1)
            separable = layer_config.get('separable', False)
            heads = layer_config.get('heads', -1)

            encoder_layers.append(
                JasperBlock(input_features,
                            layer_config['filters'],
                            repeat=layer_config['repeat'],
                            kernel_size=layer_config['kernel'],
                            stride=layer_config['stride'],
                            dilation=layer_config['dilation'],
                            dropout=layer_config['dropout'],
                            residual=layer_config['residual'],
                            groups=groups,
                            separable=separable,
                            heads=heads,
                            normalization=normalization_mode,
                            norm_groups=norm_groups,
                            activation=activation,
                            residual_panes=dense_res,
                            residual_mode=residual_mode,
                            conv_mask=self.use_conv_mask)
            )
            input_features = layer_config['filters']

        self._output_dim = input_features
        self.encoder = nn.Sequential(*encoder_layers)
        self.apply(lambda x: init_weights(x, mode=init_mode))

    def input_dim(self):
        return self._input_dim

    def output_dim(self):
        return self._output_dim

    def num_weights(self):
        return sum(p.numel() for p in self.parameters() if p.requires_grad)

    def forward(self, audio_signal, length):
        s_input, length = self.encoder(([audio_signal], length))
        return s_input[-1], length


class JasperDecoderForCTC(nn.Module):
    """Jasper decoder
    """

    def __init__(self, input_dim, dictionary, **kwargs):
        nn.Module.__init__(self)
        self._feat_in = input_dim
        self._num_classes = len(dictionary)
        init_mode = kwargs.get('init_mode', 'xavier_uniform')

        self.decoder_layers = nn.Sequential(
            nn.Conv1d(self._feat_in, self._num_classes, kernel_size=1, bias=True), )
        self.apply(lambda x: init_weights(x, mode=init_mode))

    def output_dim(self):
        return self._num_classes

    def num_weights(self):
        return sum(p.numel() for p in self.parameters() if p.requires_grad)

    def forward(self, encoder_output):
        out = self.decoder_layers(encoder_output).transpose(1, 2)
        return out


class MaskedConv1d(nn.Module):
    __constants__ = ["use_conv_mask", "real_out_channels", "heads"]

    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1, heads=-1, bias=False,
                 use_mask=True):
        super(MaskedConv1d, self).__init__()

        if not (heads == -1 or groups == in_channels):
            raise ValueError("Only use heads for depthwise convolutions")

        self.real_out_channels = out_channels
        if heads != -1:
            in_channels = heads
            out_channels = heads
            groups = heads

        self.conv = nn.Conv1d(in_channels, out_channels,
                              kernel_size,
                              stride=stride,
                              padding=padding, dilation=dilation,
                              groups=groups, bias=bias)
        self.use_mask = use_mask
        self.heads = heads

    def get_seq_len(self, lens):
        return ((lens + 2 * self.conv.padding[0] - self.conv.dilation[0] * (
            self.conv.kernel_size[0] - 1) - 1) // self.conv.stride[0] + 1)

    def forward(self, x, lens=None):
        if self.use_mask:
            assert lens is not None
            lens = lens.to(dtype=torch.long)
            max_len = x.size(2)
            mask = torch.arange(max_len).to(lens.device) \
                       .expand(len(lens), max_len) >= lens.unsqueeze(1)
            x = x.masked_fill(
                mask.unsqueeze(1).to(device=x.device), 0
            )
            # del mask
            lens = self.get_seq_len(lens)
        else:
            assert lens is None

        sh = x.shape
        if self.heads != -1:
            x = x.view(-1, self.heads, sh[-1])

        out = self.conv(x)

        if self.heads != -1:
            out = out.view(sh[0], self.real_out_channels, -1)

        return out, lens


class GroupShuffle(nn.Module):

    def __init__(self, groups, channels):
        super(GroupShuffle, self).__init__()

        self.groups = groups
        self.channels_per_group = channels // groups

    def forward(self, x):
        sh = x.shape

        x = x.view(-1, self.groups, self.channels_per_group, sh[-1])

        x = torch.transpose(x, 1, 2).contiguous()

        x = x.view(-1, self.groups * self.channels_per_group, sh[-1])

        return x


class JasperBlock(nn.Module):
    __constants__ = ["conv_mask", "separable", "residual_mode", "res", "mconv"]

    def __init__(self, inplanes, planes, repeat=3, kernel_size=11, stride=1,
                 dilation=1, padding='same', dropout=0.2, activation=None,
                 residual=True, groups=1, separable=False,
                 heads=-1, normalization="batch",
                 norm_groups=1, residual_mode='add',
                 residual_panes=[], conv_mask=False):
        super(JasperBlock, self).__init__()

        if padding != "same":
            raise ValueError("currently only 'same' padding is supported")

        padding_val = get_same_padding(kernel_size[0], stride[0], dilation[0])
        self.conv_mask = conv_mask
        self.separable = separable
        self.residual_mode = residual_mode

        inplanes_loop = inplanes
        conv = nn.ModuleList()

        for _ in range(repeat - 1):
            conv.extend(self._get_conv_bn_layer(
                inplanes_loop,
                planes,
                kernel_size=kernel_size,
                stride=stride,
                dilation=dilation,
                padding=padding_val,
                groups=groups,
                heads=heads,
                separable=separable,
                normalization=normalization,
                norm_groups=norm_groups))

            conv.extend(self._get_act_dropout_layer(
                drop_prob=dropout,
                activation=activation))

            inplanes_loop = planes

        conv.extend(self._get_conv_bn_layer(
            inplanes_loop,
            planes,
            kernel_size=kernel_size,
            stride=stride,
            dilation=dilation,
            padding=padding_val,
            groups=groups,
            heads=heads,
            separable=separable,
            normalization=normalization,
            norm_groups=norm_groups))

        self.mconv = conv

        res_panes = residual_panes.copy()
        self.dense_residual = residual

        if residual:
            res_list = nn.ModuleList()
            if len(residual_panes) == 0:
                res_panes = [inplanes]
                self.dense_residual = False
            for ip in res_panes:
                res_list.append(nn.ModuleList(self._get_conv_bn_layer(
                    ip,
                    planes,
                    kernel_size=1,
                    normalization=normalization,
                    norm_groups=norm_groups)))
            self.res = res_list
        else:
            self.res = None

        self.mout = nn.Sequential(
            *self._get_act_dropout_layer(
                drop_prob=dropout,
                activation=activation)
        )

    def _get_conv(self, in_channels, out_channels, kernel_size=11,
                  stride=1, dilation=1, padding=0, bias=False,
                  groups=1, heads=-1, separable=False):
        use_mask = self.conv_mask
        return MaskedConv1d(in_channels, out_channels, kernel_size,
                            stride=stride,
                            dilation=dilation, padding=padding, bias=bias,
                            groups=groups, heads=heads,
                            use_mask=use_mask)

    def _get_conv_bn_layer(self, in_channels,
                           out_channels,
                           kernel_size=11,
                           stride=1,
                           dilation=1,
                           padding=0,
                           bias=False,
                           groups=1,
                           heads=-1,
                           separable=False,
                           normalization="batch",
                           norm_groups=1):
        if norm_groups == -1:
            norm_groups = out_channels

        if separable:
            layers = [
                self._get_conv(in_channels, in_channels, kernel_size,
                               stride=stride,
                               dilation=dilation, padding=padding, bias=bias,
                               groups=in_channels, heads=heads),
                self._get_conv(in_channels, out_channels,
                               kernel_size=1,
                               stride=1,
                               dilation=1, padding=0, bias=bias, groups=groups)
            ]
        else:
            layers = [
                self._get_conv(in_channels, out_channels, kernel_size,
                               stride=stride,
                               dilation=dilation, padding=padding, bias=bias,
                               groups=groups)
            ]

        if normalization == "group":
            layers.append(nn.GroupNorm(
                num_groups=norm_groups, num_channels=out_channels))
        elif normalization == "instance":
            layers.append(nn.GroupNorm(
                num_groups=out_channels, num_channels=out_channels))
        elif normalization == "layer":
            layers.append(nn.GroupNorm(
                num_groups=1, num_channels=out_channels))
        elif normalization == "batch":
            layers.append(nn.BatchNorm1d(out_channels, eps=1e-3, momentum=0.1))
        else:
            raise ValueError(
                f"Normalization method ({normalization}) does not match"
                f" one of [batch, layer, group, instance].")

        if groups > 1:
            layers.append(GroupShuffle(groups, out_channels))
        return layers

    def _get_act_dropout_layer(self, drop_prob=0.2, activation=None):
        if activation is None:
            activation = nn.Hardtanh(min_val=0.0, max_val=20.0)
        layers = [
            activation,
            nn.Dropout(p=drop_prob)
        ]
        return layers

    def forward(self, input_):
        xs, lens_orig = input_
        # compute forward convolutions
        out = xs[-1]

        lens = lens_orig
        for i, l in enumerate(self.mconv):
            # if we're doing masked convolutions, we need to pass in and
            # possibly update the sequence lengths
            # if (i % 4) == 0 and self.conv_mask:
            if isinstance(l, MaskedConv1d):
                out, lens = l(out, lens)
            else:
                out = l(out)

        # compute the residuals
        if self.res is not None:
            for i, layer in enumerate(self.res):
                res_out = xs[i]
                for j, res_layer in enumerate(layer):
                    if isinstance(res_layer, MaskedConv1d):
                        res_out, _ = res_layer(res_out, lens_orig)
                    else:
                        res_out = res_layer(res_out)

                if self.residual_mode == 'add':
                    out = out + res_out
                else:
                    out = torch.max(out, res_out)

        # compute the output
        out = self.mout(out)
        if self.res is not None and self.dense_residual:
            return xs + [out], lens

        return [out], lens
