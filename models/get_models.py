import torch
import numpy as np
import pickle
from os.path import join
from torch.distributions.categorical import Categorical

from models.egnn_blocks import EGNN_dynamics, EGNN_encoder, EGNN_decoder
from models.egnn_vae import EnHierarchicalVAE
from models.goat import GeometricOptimalTransportFlow

def get_autoencoder(args, device, dataset_info):
    in_node_nf = len(dataset_info['atom_decoder']) + int(args.include_charges)
    # print('Autoencoder models are _not_ conditioned on time.')

    encoder = EGNN_encoder(
        in_node_nf=in_node_nf, context_node_nf=args.context_node_nf, out_node_nf=args.latent_nf,
        n_dims=3, device=device, hidden_nf=args.nf,
        act_fn=torch.nn.SiLU(), n_layers=1,
        attention=args.attention, tanh=args.tanh, mode=args.model, norm_constant=args.norm_constant,
        inv_sublayers=args.inv_sublayers, sin_embedding=args.sin_embedding,
        normalization_factor=args.normalization_factor, aggregation_method=args.aggregation_method,
        include_charges=args.include_charges
    )

    decoder = EGNN_decoder(
        in_node_nf=args.latent_nf, context_node_nf=args.context_node_nf, out_node_nf=in_node_nf,
        n_dims=3, device=device, hidden_nf=args.nf,
        act_fn=torch.nn.SiLU(), n_layers=args.n_layers,
        attention=args.attention, tanh=args.tanh, mode=args.model, norm_constant=args.norm_constant,
        inv_sublayers=args.inv_sublayers, sin_embedding=args.sin_embedding,
        normalization_factor=args.normalization_factor, aggregation_method=args.aggregation_method,
        include_charges=args.include_charges
    )

    vae = EnHierarchicalVAE(
        encoder=encoder,
        decoder=decoder,
        in_node_nf=in_node_nf,
        n_dims=3,
        latent_node_nf=args.latent_nf,
        kl_weight=args.kl_weight,
        norm_values=args.normalize_factors,
        include_charges=args.include_charges
    )

    return vae

def get_args_gen(dir_path, train_epoch=''):
    with open(join(dir_path, 'args%s.pickle' % train_epoch), 'rb') as f:
        args_gen = pickle.load(f)
    assert args_gen.dataset == 'qm9_second_half'

    # Add missing args!
    if not hasattr(args_gen, 'normalization_factor'):
        args_gen.normalization_factor = 1
    if not hasattr(args_gen, 'aggregation_method'):
        args_gen.aggregation_method = 'sum'
    return args_gen

def get_goat(args, device, dataset_info, dataloader_train):
    histogram = dataset_info['n_nodes']
    in_node_nf = len(dataset_info['atom_decoder']) + int(args.include_charges)
    nodes_dist = DistributionNodes(histogram)

    prop_dist = None
    if len(args.conditioning) > 0:
        prop_dist = DistributionProperty(dataloader_train, args.conditioning)

    args.context_node_nf = len(args.conditioning)
    first_stage_args = args

    # CAREFUL with this -->
    if not hasattr(first_stage_args, 'normalization_factor'):
        first_stage_args.normalization_factor = 1
    if not hasattr(first_stage_args, 'aggregation_method'):
        first_stage_args.aggregation_method = 'sum'

    # TODO May VAE based on transformer would be better?
    feature_model = get_autoencoder(first_stage_args, device, dataset_info)
    in_node_nf = args.latent_nf
    print(device)
    if args.probabilistic_model == 'vae':
        return feature_model, nodes_dist, prop_dist
    if args.vae_path is not None:
        feature_model_dict = torch.load(join(args.vae_path), map_location='cpu')
        feature_model.load_state_dict(feature_model_dict)
        print('load from ', args.vae_path)

    if args.condition_time:
        dynamics_in_node_nf = in_node_nf + 1
    else:
        print('Warning: dynamics model is _not_ conditioned on time.')
        dynamics_in_node_nf = in_node_nf
    net_dynamics = EGNN_dynamics(
        in_node_nf=dynamics_in_node_nf, context_node_nf=args.context_node_nf,
        n_dims=3, device=device, hidden_nf=args.nf,
        act_fn=torch.nn.SiLU(), n_layers=args.n_layers,
        attention=args.attention, tanh=args.tanh, mode=args.model, norm_constant=args.norm_constant,
        inv_sublayers=args.inv_sublayers, sin_embedding=args.sin_embedding,
        normalization_factor=args.normalization_factor, aggregation_method=args.aggregation_method)
    if not hasattr(args, 'distill'):
        args.distill = False

    flow = GeometricOptimalTransportFlow(
        dynamics=net_dynamics,
        in_node_nf=in_node_nf,
        n_dims=3,
        vae=feature_model,
        timesteps=args.diffusion_steps,
        loss_type=args.diffusion_loss_type,
        norm_values=args.normalize_factors,
        include_charges=args.include_charges,
        discrete_path=args.discrete_path,
        trainable_ae=args.trainable_ae,
        device=device,
        distill=args.distill,
    )

    return flow, nodes_dist, prop_dist


def get_optim(args, generative_model):
    optim = torch.optim.AdamW(
        generative_model.parameters(),
        lr=args.lr, amsgrad=True,
        weight_decay=1e-12)

    return optim


class DistributionNodes:
    def __init__(self, histogram):
        self.n_nodes = []
        prob = []
        self.keys = {}
        for i, nodes in enumerate(histogram):
            self.n_nodes.append(nodes)
            self.keys[nodes] = i
            prob.append(histogram[nodes])
        self.n_nodes = torch.tensor(self.n_nodes)
        prob = np.array(prob)
        prob = prob / np.sum(prob)

        self.prob = torch.from_numpy(prob).float()

        entropy = torch.sum(self.prob * torch.log(self.prob + 1e-30))
        # print("Entropy of n_nodes: H[N]", entropy.item())

        self.m = Categorical(torch.tensor(prob))

    def sample(self, n_samples=1):
        idx = self.m.sample((n_samples,))
        return self.n_nodes[idx]

    def log_prob(self, batch_n_nodes):
        assert len(batch_n_nodes.size()) == 1

        idcs = [self.keys[i.item()] for i in batch_n_nodes]
        idcs = torch.tensor(idcs).to(batch_n_nodes.device)

        log_p = torch.log(self.prob + 1e-30)

        log_p = log_p.to(batch_n_nodes.device)

        log_probs = log_p[idcs]

        return log_probs


class DistributionProperty:
    def __init__(self, dataloader, properties, num_bins=1000, normalizer=None):
        self.num_bins = num_bins
        self.distributions = {}
        self.properties = properties
        for prop in properties:
            self.distributions[prop] = {}
            self._create_prob_dist(dataloader.dataset.data['num_atoms'],
                                   dataloader.dataset.data[prop],
                                   self.distributions[prop])

        self.normalizer = normalizer

    def set_normalizer(self, normalizer):
        self.normalizer = normalizer

    def _create_prob_dist(self, nodes_arr, values, distribution):
        min_nodes, max_nodes = torch.min(nodes_arr), torch.max(nodes_arr)
        for n_nodes in range(int(min_nodes), int(max_nodes) + 1):
            idxs = nodes_arr == n_nodes
            values_filtered = values[idxs]
            if len(values_filtered) > 0:
                probs, params = self._create_prob_given_nodes(values_filtered)
                distribution[n_nodes] = {'probs': probs, 'params': params}

    def _create_prob_given_nodes(self, values):
        n_bins = self.num_bins  # min(self.num_bins, len(values))
        prop_min, prop_max = torch.min(values), torch.max(values)
        prop_range = prop_max - prop_min + 1e-12
        histogram = torch.zeros(n_bins)
        for val in values:
            i = int((val - prop_min) / prop_range * n_bins)
            # Because of numerical precision, one sample can fall in bin int(n_bins) instead of int(n_bins-1)
            # We move it to bin int(n_bind-1 if tat happens)
            if i == n_bins:
                i = n_bins - 1
            histogram[i] += 1
        probs = histogram / torch.sum(histogram)
        probs = Categorical(torch.tensor(probs))
        params = [prop_min, prop_max]
        return probs, params

    def normalize_tensor(self, tensor, prop):
        assert self.normalizer is not None
        mean = self.normalizer[prop]['mean']
        mad = self.normalizer[prop]['mad']
        return (tensor - mean) / mad

    def sample(self, n_nodes=19):
        vals = []
        for prop in self.properties:
            dist = self.distributions[prop][n_nodes]
            idx = dist['probs'].sample((1,))
            val = self._idx2value(idx, dist['params'], len(dist['probs'].probs))
            val = self.normalize_tensor(val, prop)
            vals.append(val)
        vals = torch.cat(vals)
        return vals

    def sample_batch(self, nodesxsample):
        vals = []
        for n_nodes in nodesxsample:
            vals.append(self.sample(int(n_nodes)).unsqueeze(0))
        vals = torch.cat(vals, dim=0)
        return vals

    def _idx2value(self, idx, params, n_bins):
        prop_range = params[1] - params[0]
        left = float(idx) / n_bins * prop_range + params[0]
        right = float(idx + 1) / n_bins * prop_range + params[0]
        val = torch.rand(1) * (right - left) + left
        return val


if __name__ == '__main__':
    dist_nodes = DistributionNodes()
    print(dist_nodes.n_nodes)
    print(dist_nodes.prob)
    for i in range(10):
        print(dist_nodes.sample())
