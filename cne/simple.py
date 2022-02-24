import torch
import numpy as np

from .cne import ContrastiveEmbedding
from annoy import AnnoyIndex
from scipy.sparse import lil_matrix
from sklearn.decomposition import PCA

class NeighborTransformData(torch.utils.data.Dataset):
    """Returns a pair of neighboring points in the dataset."""

    def __init__(
            self, dataset, neighbor_mat, random_state=None
    ):
        self.dataset = torch.tensor(dataset, dtype=torch.float32)
        self.neighbor_mat = neighbor_mat
        self.rng = np.random.default_rng(random_state)

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, i):
        neighs = self.neighbor_mat[i].nonzero()[1]
        nidx = self.rng.choice(neighs)

        item = self.dataset[i]
        neigh = self.dataset[nidx]
        return item, neigh


class NeighborTransformIndices(torch.utils.data.Dataset):
    """Returns a pair of indices of neighboring points in the dataset."""

    def __init__(
            self, neighbor_mat, random_state=None
    ):
        neighbor_mat = neighbor_mat.tocoo()
        self.heads = torch.tensor(neighbor_mat.row)
        self.tails = torch.tensor(neighbor_mat.col)

    def __len__(self):
        return len(self.heads)

    def __getitem__(self, i):
        return self.heads[i], self.tails[i]



class NumpyToTensorDataset(torch.utils.data.Dataset):
    def __init__(self, dataset, reshape=None):
        self.dataset = torch.tensor(dataset, dtype=torch.float32)
        if reshape is not None:
            self.reshape = lambda x: np.reshape(x, reshape)
        else:
            self.reshape = lambda x: x

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, i):
        item = self.dataset[i]
        return self.reshape(item)


class NumpyToIndicesDataset(torch.utils.data.Dataset):
    def __init__(self, n):
        self.n = n

    def __len__(self):
        return self.n

    def __getitem__(self, i):
        return i


class FCNetwork(torch.nn.Module):
    "Fully-connected network"

    def __init__(self, in_dim=784, feat_dim=2):
        super(FCNetwork, self).__init__()
        self.flatten = torch.nn.Flatten()
        self.linear_relu_stack = torch.nn.Sequential(
            torch.nn.Linear(in_dim, 100),
            torch.nn.ReLU(),
            torch.nn.Linear(100, 100),
            torch.nn.ReLU(),
            torch.nn.Linear(100, 100),
            torch.nn.ReLU(),
            torch.nn.Linear(100, feat_dim),
        )

    def forward(self, x):
        x = self.flatten(x)
        logits = self.linear_relu_stack(x)
        return logits


class CNE(object):
    def __init__(self, model=None, k=15, parametric=True, **kwargs):
        self.model = model
        self.k = k
        self.parametric = parametric
        # self.batch_size = batch_size
        # self.num_workers = num_workers
        self.kwargs = kwargs


    def fit_transform(self, X, y=None):
        self.fit(X, y)

        if self.parametric:
            self.dataset_plain = NumpyToTensorDataset(X)
        else:
            self.dataset_plain = NumpyToIndicesDataset(len(X))

        self.dl_unshuf = torch.utils.data.DataLoader(
            self.dataset_plain,
            shuffle=False,
            batch_size=self.cne.batch_size,
        )
        model = self.cne.model
        device = self.cne.device
        ar = np.vstack([model(batch.to(device))
                        .cpu().detach().numpy()
                        for batch in self.dl_unshuf])
        return ar

    def fit(self, X, y=None, init=None):

        in_dim = X.shape[1]
        if self.model is None:
            if self.parametric:
                self.model = FCNetwork(in_dim)
            else:
                if init is None:
                    # default to pca
                    pca_projector = PCA(n_components=2)
                    init = pca_projector.fit_transform(X)
                    init /= (init[:, 0].std())
                elif isinstance(init, np.array):
                    assert len(init) == len(X),f"Data and initialization must have the same number of elements but have {len(X)} and {len(init)}."
                    assert len(init.shape) == 2, f"Initialization must have 2 dimensions but has {len(init.shape)}."
                # All embedding parameters will be part of the model. This is
                # conceptually easy, but limits us to embeddings that fit on the
                # GPU.
                self.model = torch.nn.Embedding.from_pretrained(torch.tensor(init))
                self.model.requires_grad_(True)

        # use higher learning rate for non-parametric version
        lr = 0.001 if self.parametric else 0.1
        self.cne = ContrastiveEmbedding(self.model, learning_rate=lr, **self.kwargs)

        # create approximate NN search tree
        self.annoy = AnnoyIndex(in_dim, "euclidean")
        [self.annoy.add_item(i, x) for i, x in enumerate(X)]
        self.annoy.build(50)

        # construct the adjacency matrix for the graph
        adj = lil_matrix((X.shape[0], X.shape[0]))
        for i in range(X.shape[0]):
            neighs_, dists_ = self.annoy.get_nns_by_item(i, self.k + 1, include_distances=True)
            neighs = neighs_[1:]
            dists = dists_[1:]

            adj[i, neighs] = 1
            adj[neighs, i] = 1  # symmetrize on the fly

        self.neighbor_mat = adj.tocsr()

        data_seed = 33
        if self.parametric:
            self.dataset = NeighborTransformData(X, self.neighbor_mat, data_seed)
        else:
            self.dataset = NeighborTransformIndices(self.neighbor_mat)

        seed = 6267340091634178711
        gen = torch.Generator().manual_seed(seed)
        self.dataloader = torch.utils.data.DataLoader(
            self.dataset,
            shuffle=True,
            batch_size=self.cne.batch_size,
            generator=gen,
        )

        self.cne.fit(self.dataloader, len(X))
        return self
