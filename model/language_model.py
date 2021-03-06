
# encoding=utf-8
import math
import copy
from typing import Optional,List
import torch
from torch import Tensor, device
import torch.nn as nn
import torch.nn.functional as F
from model.img_encoding import build_backbone
from model.position_encoding import PositionalEncoding

class MLP(nn.Module):
    """ Very simple multi-layer perceptron (also called FFN)"""

    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1) # [256,256]
        # test1 = [input_dim] + h # [256,256,256]
        # test2 = h + [output_dim] # [256,256,4]
        self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x


def _get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])


class LayerNorm(nn.Module):
    def __init__(self, features, eps=1e-6):
        super(LayerNorm, self).__init__()
        self.a_2 = nn.Parameter(torch.ones(features))
        self.b_2 = nn.Parameter(torch.zeros(features))
        self.eps = eps

    def forward(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.a_2 * (x - mean) / (std + self.eps) + self.b_2


##①##
# (n_batch,seq_len)-->(n_batch,seq_len,d_model)
class TokenEmbedding(nn.Module):
    def __init__(self, vocab_size: int, d_model: int):  # (32,56)-->(32,56,512),embedding_size = d_model != vocab_size
        super(TokenEmbedding, self).__init__()
        self.embedding = nn.Embedding(
            vocab_size,
            d_model,
        )
        self.d_model = d_model

    def forward(self, tokens: Tensor):
        return self.embedding(tokens.long()) * math.sqrt(self.d_model)


# 一开始值大(embed)，后面值小(attention)



class SublayerConnection(nn.Module):
    def __init__(self, size, dropout):
        super(SublayerConnection, self).__init__()
        self.norm = LayerNorm(size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, sublayer):
        return x + self.dropout(sublayer(self.norm(x)))
        # n*Norm
        # n*dropout      between layers

##③##
class LanguageModel(nn.Module):
    def __init__(self, encoder, args):
        super(LanguageModel, self).__init__()
        self.encoder = encoder

        self.bbox_embed = MLP(4, args.d_feedforward, args.d_model, 3)
        self.class_embed = TokenEmbedding(args.src_vocab, args.d_model)
        self.img_embed = build_backbone(args)
        self.pos_embed = PositionalEncoding(args.d_model, args.dropout)

        self.mix_net = MLP(args.d_model*3, args.d_feedforward, args.d_model, 3)
        self.generator = MLP(args.d_model, args.d_model, 4, 3)

    def forward(self, batch):
        '''
        img,    (32,x<len,)
        class,  (32,len)    [one-hot_version: (32,len,n)]
        bbox,   (32,len,4)

        img: (1284, 500 ,20) 20表示20中面部测量单元（动作单元）
        text:(1284, 50, 300)
        audio:(1284,375,5)
        '''
        class_embedding = self.class_embed(batch.label) # (32,len)->(32,len,512)
        bbox_embedding = self.bbox_embed(batch.bbox) #(32,len,4)->(32,len,512) # TODO 另一种是bbox_index，for循环选择添加，类似img的处理方式
        img_embedding = self.img_embed(batch.img) # (32,x<len,2048,4,4)->
        x = torch.cat((class_embedding,bbox_embedding,img_embedding),dim=-1)
        x = self.mix_net(x)
        x = self.pos_embed(x)
        x = self.encoder(x, batch.mask)
        x = self.generator(x).sigmoid()
        return x
        # output shape (n_batch,seq_len,d_model)；不是(n_batch,seq_len,vocab)，generator保存在模型中供调用而非参与计算。如果要生成或计算损失，还要调用计算一下。
        # 维护decoder和encoder的输出形状统一??
        # 对于decoder，src_mask还有用吗?---有用，在尝试注意memory时用到


class Encoder(nn.Module):
    def __init__(self, encoder_layer, N):  # N*encoder_layer
        super(Encoder, self).__init__()
        self.layers = _get_clones(encoder_layer, N)
        self.norm = LayerNorm(encoder_layer.size)

    def forward(self, x, mask):
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)
        # 1*Norm1    encode final



class EncoderLayer(nn.Module):
    def __init__(self, size, self_attn, fnn, dropout):  # encoder_layer = self_attention + add&norm + fnn + add&norm
        super(EncoderLayer, self).__init__()
        self.self_attn = self_attn
        self.fnn = fnn
        self.sublayer = _get_clones(SublayerConnection(size, dropout), 2)
        self.size = size

    def forward(self, x, mask):
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, mask))
        # sublayer(x,f)要求x-输入->f。对于后者无法输入而已经得到值的情况下，使用lambda接受输入，返回这个值
        return self.sublayer[1](x, self.fnn)

def attention(query, key, value, mask=None, dropout=None):
    d_k = query.size(-1)  # d_k = d_model / n_head，单个head关注的维度
    scores = torch.matmul(query, key.transpose(-2, -1)) / math.sqrt(d_k)
    # 注意只转置两个维度，因此不能直接k.T
    # (32,8,seq_len,64)*(32,8,64,seq_len)-->(seq,8,seq_len,seq_len)。这里的值由64维信息得到；如果是非multi_head，就是由512维信息得到
    if mask is not None:
        scores = scores.masked_fill(mask == 0, -1e9)  # -1*10^9
    p_attn = F.softmax(scores, dim=-1)
    if (dropout is not None):
        p_attn = dropout(p_attn)
    return torch.matmul(p_attn, value), p_attn
    # (1*dropout)



class MultiHeadAttention(nn.Module):
    def __init__(self, h, d_model, dropout=0.1):
        super(MultiHeadAttention, self).__init__()
        assert d_model % h == 0
        self.d_k = d_model // h
        self.h = h
        self.linear = _get_clones(nn.Linear(d_model,d_model), 4)
        self.attn = None
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, query, key, value, mask=None):
        if mask is not None:
            mask = mask.unsqueeze(
                1)  # src_mask:(32,1,seq_len)->(32,1,1,seq_len),前面的1在head上广播，后面的(1,seq_len)在(seq_len,seq_len)上广播
        n_batch = query.size(0)

        query, key, value = [l(x).view(n_batch, -1, self.h, self.d_k).transpose(1, 2) for l, x in
                             zip(self.linear, (query, key, value))]
        # (32,seq_len,512)-->(32, 8, seq_len, 64)
        x, self.attn = attention(query, key, value, mask=mask, dropout=self.dropout)
        # x:(n_batch,8,seq_len,64) attn:(n_batch, 8, seq_len, seq_len)
        x = x.transpose(1, 2).contiguous().view(n_batch, -1, self.h * self.d_k)
        # (32, 8, seq_len, 64)-->(32,seq_len,512)
        return self.linear[-1](x)


class PositionWiseFeedForward(nn.Module):
    def __init__(self, d_model, d_ff, dropout=0.1):
        super(PositionWiseFeedForward, self).__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, x):
        return self.w_2(self.dropout(F.relu(self.w_1(x))))



def make_model(args):
    c = copy.deepcopy
    attn = MultiHeadAttention(args.n_heads, args.d_model)                   # (h, d_model, dropout=0.1)
    ffn = PositionWiseFeedForward(args.d_model, args.d_feedforward, args.dropout)   # (d_model, d_ff, dropout=0.1)
    

    model = LanguageModel(
        Encoder(EncoderLayer(args.d_model, c(attn), c(ffn), args.dropout), args.n_encoder_layers),
        args
    )

    for p in model.parameters():
        if p.dim()>1:
            nn.init.xavier_uniform_(p)   # 初始化参数，使得每一层的方差尽可能相等。
    return model