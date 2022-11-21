from __future__ import print_function, division

import os
import warnings
import subprocess
from tqdm.auto import tqdm
import numpy as np
from sklearn.cluster import KMeans, SpectralClustering
from sklearn import decomposition
from scipy.optimize import linear_sum_assignment
from collections import OrderedDict
import shutil

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable

from core.utils import DiarizationDataSet, make_rttm
import core.optimumSpeaker as optimumSpeaker

warnings.filterwarnings('ignore')

device = "cuda" if torch.cuda.is_available() else "cpu"

class ResidualAutoEncoder(nn.Module):
    """
    Auto Encoder for encoding the input features to a lower dimension.
    """

    def __init__(self, ip_features, hidden_dims=[500, 500, 2000, 30]):
        super().__init__()
        hidden_dims = [ip_features] + hidden_dims

        i = 0
        self.en1 = nn.Linear(hidden_dims[i], hidden_dims[i+1]); i+=1
        self.en2 = nn.Linear(hidden_dims[i], hidden_dims[i+1]); i+=1
        self.en3 = nn.Linear(hidden_dims[i], hidden_dims[i+1]); i+=1
        self.en4 = nn.Linear(hidden_dims[i], hidden_dims[i+1]); i+=1
        self.encoder_layers = [self.en1, self.en2, self.en3, self.en4]

        i = 1
        self.dc1 = nn.Linear(hidden_dims[-i], hidden_dims[-(i+1)]); i+=1
        self.dc2 = nn.Linear(hidden_dims[-i], hidden_dims[-(i+1)]); i+=1
        self.dc3 = nn.Linear(hidden_dims[-i], hidden_dims[-(i+1)]); i+=1
        self.dc4 = nn.Linear(hidden_dims[-i], hidden_dims[-(i+1)]); i+=1
        self.decoder_layers = [self.dc1, self.dc2, self.dc3, self.dc4]


    def forward(self, x):
        xo = [x]
        xr = []

        for i in range(len(self.encoder_layers)):
            x = self.encoder_layers[i](x)

            if i < len(self.encoder_layers)-1:
                x = F.relu(x)
                xo.append(x + 1.0 - 1.0)
            else:
                z = x + 1.0 - 1.0

        for i in range(len(self.decoder_layers)):
            x = self.decoder_layers[i](x)
            if i < len(self.decoder_layers)-1:
                x = F.relu(x)

            xr.append(x + 1.0 - 1.0)

        xr.reverse()

        return z, xo, xr


def load_encoder():
    if 'ResAE_Model_III.pth' not in os.listdir("./"):
        print("Downloading pre-trained weights for auto encoder...")
        # subprocess.check_output(["gdown", "--id", "1SI1GOCDnzbZRicm-2toeY2AfkYdbygQc"]) # [500, 500, 2000, 30]
        subprocess.check_output(["gdown", "--id", "10FRT4V5-fanAasMYzG3IPnE7Ocp-e6gv"]) # ResAE_Model_III
        # subprocess.check_output(["gdown", "--id", "1bjccsVgK2B98QdGwGa3MxlFBpsWgbAGT"]) # ResAE_Model_III_FT
        # subprocess.check_output(["gdown", "--id", "1wX1DLxHn74VV50JTwY4trz5P8cw3v8Cl"]) # [128, 128, 512, 64]
        print("Downloading Complete!\n")

    model = ResidualAutoEncoder(ip_features=192, hidden_dims=[500, 500, 2000, 30])
    weights = torch.load('./ResAE_Model_III.pth')
    model.load_state_dict(weights['state_dict'])
    model = model.to(device)

    return model

class ClusteringModule(nn.Module):
    def __init__(self, num_clusters, encoder, data, cinit="KMeans"):
        super().__init__()
        self.encoder = encoder
        self.num_clusters = num_clusters

        cluster_centers_init = self.init_centroid(data, method=cinit)
        self.cluster_centers = nn.Parameter(cluster_centers_init, requires_grad=True)

    def forward(self, x):
        '''
        Extract latent-space vectors
        '''

        z, xo, xr = self.encoder(x)

        d2 = torch.sum((z.unsqueeze(1) - self.cluster_centers)**2, axis=2)
        q = 1/(1+d2)
        q = q/torch.sum(q, axis=1, keepdim=True)

        p = (q**2)/torch.sum(q, axis=0, keepdim=True)
        p = p/torch.sum(p, axis=1, keepdim=True)

        return q, p, xo[0], xr[0]

    def init_centroid(self, data, method="KMeans"):
        '''
        To find the initial centroid for DEC clustering module
        Two option avaialble method="KMeans" or "Spectral"
        '''

        z_init, _, _ = self.encoder(data.to(device))

        if self.num_clusters == None:
            EGNC = optimumSpeaker.eigenGap(p_percentile=0.9, gaussian_blur_sigma=2)
            n1 = EGNC.find(z_init.detach().cpu().numpy())
            EGNC = optimumSpeaker.eigenGap(p_percentile=0.95, gaussian_blur_sigma=2)
            n2 = EGNC.find(z_init.detach().cpu().numpy())

            self.num_clusters = max(n1, n2)
            # self.num_clusters = max(n1, 0)

        Xt = z_init.detach().cpu().numpy()
        X = Xt - Xt.mean(axis=0)
        X = X/X.std(axis=0)

        pca = decomposition.PCA(n_components=min(self.num_clusters, X.shape[1]))
        pca.fit(X)
        X_pca = pca.transform(X)

        if method=="Spectral" and self.num_clusters > 1:
            clustering = SpectralClustering(n_clusters=self.num_clusters,
                                            assign_labels="discretize")

        elif method=="KMeans" or self.num_clusters == 1:
            clustering = KMeans(n_clusters=self.num_clusters,
                                init="k-means++",
                                max_iter=300)

        plabels = clustering.fit_predict(X_pca)

        cluster_centers = []
        for i in np.unique(plabels):
            idx = list(np.argwhere(plabels==i).reshape(-1))
            cluster_centers.append(np.mean(z_init[idx].detach().cpu().numpy(), axis=0))

        cluster_centers = np.array(cluster_centers)
        return torch.from_numpy(cluster_centers).to(device)

class DEC:
    def __init__(self, encoder, num_clusters=None, cinit="KMeans"):
        self.encoder = encoder
        for param in self.encoder.parameters():
            param.requires_grad=True

        self.num_clusters = num_clusters
        self.criterion = nn.KLDivLoss()
        self.criterionRec = nn.MSELoss()
        self.cm = None
        self.cinit = cinit

    def fit(self, data, y_true=None, niter=150, lrEnc=1e-4, lrCC=1e-4, verbose=False):
        '''
        Refine the cluster assignment on the chosen audio data.
        '''

        data = data.to(device)

        if self.cm == None:
            self.cm = ClusteringModule(self.num_clusters, self.encoder, data, cinit=self.cinit)
            self.cm = self.cm.to(device)

        optimizerEnc = optim.Adam(self.cm.encoder.parameters(), lr=lrEnc)
        optimizerCC = optim.Adam([self.cm.cluster_centers], lr=lrCC)

        if verbose:
            epochProgress = tqdm(range(niter), leave=True, ncols=750)
        else:
            epochProgress = range(niter)

        for epoch in epochProgress:
            # optimizer.zero_grad()
            optimizerEnc.zero_grad()
            optimizerCC.zero_grad()

            q, p, x, xr = self.cm(data)
            loss = self.criterion(q.log(), p.detach()) + (1/300)*self.criterionRec(x, xr)
            loss.backward()
            optimizerEnc.step()
            optimizerCC.step()

            verbose_text = "Niter " + str(epoch+1) + "/" + str(niter) + " - train_loss: " + str(round(loss.item(), 3))

            if y_true is not None:
                y_pred = self.predict(data)
                acc, _ = self.clusterAccuracy(y_pred, y_true)
                verbose_text +=  " - train_acc: " + str(round(acc, 3))

            if verbose:
                epochProgress.set_description(verbose_text, refresh=True)

    def predict(self, data):
        '''
        Predict the clusters.
        '''

        data = data.to(device)
        with torch.no_grad():
            q, p, _, _ = self.cm(data)
            _, y_pred = torch.max(p, axis=1)
            y_pred = y_pred.detach().cpu().numpy()

        return y_pred

    def clusterAccuracy(self, y_pred, y_true):
        """
        Compute the clustering accuracy by using linear sum assignment on cluster numbers.
        """

        N = max(y_pred.max(), y_true.max()) + 1

        Cm = np.zeros((N, N), dtype=np.int64)

        for i in range(y_pred.shape[0]):
            Cm[y_pred[i], y_true[i]] += 1

        row_ind, col_ind = linear_sum_assignment(Cm.max() - Cm)
        reassignment = dict(zip(row_ind, col_ind))
        accuracy = Cm[row_ind, col_ind].sum()/y_pred.shape[0]

        return accuracy, reassignment


def diarizationDEC(audio_dataset, num_spkr=None, hypothesis_dir="./rttm_output/"):
    '''
    Compute diarization labels using DEC for the audio files present inside the audio_dataset.

    If num_spkr == None then it will use eigengap to find the optimal number of speakers.
    If num_spkr == "oracle" it will use the oracle number of speakers.
    '''

    try:
        shutil.rmtree(hypothesis_dir)
    except:
        pass

    os.makedirs(hypothesis_dir, exist_ok=True)

    for i in range(len(audio_dataset)):
        # Get data sample
        audio_segments, diarization_segments, speech_segments, rttm_path = audio_dataset[i]

        # extract indexes where vad labelles the audio as speech signal
        speech_idx = np.argwhere(speech_segments==1).reshape(-1)

        # Data centering
        Xdata = audio_segments[speech_idx]

        # Load the pre-trained encoder
        encoder_pretrained = load_encoder()

        # Load DEC clustering module
        if num_spkr == "oracle":
            num_clusters = diarization_segments.shape[1]
        else:
            num_clusters = None

        decClusterer = DEC(encoder=encoder_pretrained, cinit="Spectral", num_clusters=1)
        decClusterer.fit(Xdata, niter=100, lrEnc=1e-3, lrCC=1e-1*0)

        decClusterer = DEC(encoder=encoder_pretrained, cinit="Spectral", num_clusters=num_clusters)
        decClusterer.fit(Xdata, niter=50, lrEnc=1e-3, lrCC=1e-1)

        # Applying cluster labels
        plabels = decClusterer.predict(Xdata)
        torch.cuda.empty_cache()
        print('Cache Released')

        # assign "-1" to non speech regions and cluster labels to speech regions
        diarization_prediction = np.zeros(diarization_segments.shape[0]+1)-1
        diarization_prediction[:-1][speech_idx] = plabels.copy()

        # Create RTTM file to compute DER with original diarization result
        name = rttm_path.split(sep="/")[-1][:-5]
        rttm_path_h = make_rttm(hypothesis_dir, name, diarization_prediction, audio_dataset.win_step)

    return hypothesis_dir
