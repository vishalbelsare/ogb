import argparse

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

from torch_geometric.nn.inits import glorot
from torch_scatter import scatter_mean

from ogb.linkproppred.dataset_pyg import PygLinkPropPredDataset
from ogb.linkproppred import Evaluator

from logger import Logger


class LinkPredictor(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels, out_channels, num_layers,
                 dropout):
        super(LinkPredictor, self).__init__()

        self.lins = torch.nn.ModuleList()
        self.lins.append(torch.nn.Linear(in_channels, hidden_channels))
        self.norms = torch.nn.ModuleList()
        self.norms.append(torch.nn.BatchNorm1d(hidden_channels))
        for _ in range(num_layers - 2):
            self.lins.append(torch.nn.Linear(hidden_channels, hidden_channels))
            self.norms.append(torch.nn.BatchNorm1d(hidden_channels))
        self.lins.append(torch.nn.Linear(hidden_channels, out_channels))

        self.dropout = dropout

    def reset_parameters(self):
        for lin in self.lins:
            lin.reset_parameters()
        for norm in self.norms:
            norm.reset_parameters()

    def forward(self, x_i, x_j):
        x = x_i * x_j
        for lin, norm in zip(self.lins[:-1], self.norms):
            x = lin(x)
            x = F.relu(x)
            x = F.dropout(x, p=self.dropout, training=self.training)
        x = self.lins[-1](x)
        return torch.sigmoid(x)


def train(x, predictor, splitted_edge, optimizer, batch_size):
    predictor.train()

    pos_train_edge = splitted_edge['train_edge'].to(x.weight.device)

    total_loss = total_examples = 0
    for perm in DataLoader(range(pos_train_edge.size(0)), batch_size,
                           shuffle=True):
        optimizer.zero_grad()

        edge = pos_train_edge[perm].t()
        pos_out = predictor(x(edge[0]), x(edge[1]))
        pos_loss = -torch.log(pos_out + 1e-15).mean()

        # Just do some trivial random sampling.
        neg_per_pos = 2
        edge = torch.randint(0, x.weight.size(0), (2, neg_per_pos * edge.size(1)),
                                       dtype=torch.long, device=x.weight.device)
        neg_out = predictor(x(edge[0]), x(edge[1]))
        neg_loss = -torch.log(1 - neg_out + 1e-15).mean()

        loss = pos_loss + neg_loss
        print(loss.item())
        loss.backward()
        optimizer.step()

        num_examples = pos_out.size(0)
        total_loss += loss.item() * num_examples
        total_examples += num_examples

    return total_loss / total_examples


@torch.no_grad()
def test(x, predictor, splitted_edge, evaluator, batch_size):
    predictor.eval()

    valid_edge = splitted_edge['valid_edge'].to(x.weight.device)
    test_edge = splitted_edge['test_edge'].to(x.weight.device)
    pos_train_edge = splitted_edge['train_edge'].to(x.weight.device)

    pos_train_preds = []
    for perm in DataLoader(range(pos_train_edge.size(0)),
                           batch_size=batch_size):
        edge = pos_train_edge[perm].t()
        pos_train_preds += [predictor(x(edge[0]), x(edge[1])).squeeze().cpu()]

    valid_preds = []
    for perm in DataLoader(range(valid_edge.size(0)), batch_size=batch_size):
        edge = valid_edge[perm].t()
        valid_preds += [predictor(x(edge[0]), x(edge[1])).squeeze().cpu()]

    test_preds = []
    for perm in DataLoader(range(test_edge.size(0)), batch_size=batch_size):
        edge = test_edge[perm].t()
        test_preds += [predictor(x(edge[0]), x(edge[1])).squeeze().cpu()]

    pos_train_pred = torch.cat(pos_train_preds, dim=0)

    valid_pred = torch.cat(valid_preds, dim=0)
    pos_valid_pred = valid_pred[splitted_edge['valid_edge_label'] == 1]
    neg_valid_pred = valid_pred[splitted_edge['valid_edge_label'] == 0]

    test_pred = torch.cat(test_preds, dim=0)
    pos_test_pred = test_pred[splitted_edge['test_edge_label'] == 1]
    neg_test_pred = test_pred[splitted_edge['test_edge_label'] == 0]

    results = {}
    for K in [10, 50, 100]:
        evaluator.K = K
        train_hits = evaluator.eval({
            'y_pred_pos': pos_train_pred,
            'y_pred_neg': neg_valid_pred,
        })[f'hits@{K}']
        valid_hits = evaluator.eval({
            'y_pred_pos': pos_valid_pred,
            'y_pred_neg': neg_valid_pred,
        })[f'hits@{K}']
        test_hits = evaluator.eval({
            'y_pred_pos': pos_test_pred,
            'y_pred_neg': neg_test_pred,
        })[f'hits@{K}']

        results[f'Hits@{K}'] = (train_hits, valid_hits, test_hits)

    return results


def main():
    parser = argparse.ArgumentParser(description='OGBL-PPA (MLP)')
    parser.add_argument('--device', type=int, default=0)
    parser.add_argument('--log_steps', type=int, default=1)
    parser.add_argument('--num_layers', type=int, default=3)
    parser.add_argument('--hidden_channels', type=int, default=256)
    parser.add_argument('--dropout', type=float, default=0.0)
    parser.add_argument('--batch_size', type=int, default=64 * 1024)
    parser.add_argument('--lr', type=float, default=0.005)
    parser.add_argument('--epochs', type=int, default=1000)
    parser.add_argument('--eval_steps', type=int, default=1)
    parser.add_argument('--runs', type=int, default=10)
    args = parser.parse_args()
    print(args)

    device = f'cuda:{args.device}' if torch.cuda.is_available() else 'cpu'
    device = torch.device(device)

    dataset = PygLinkPropPredDataset(name='ogbl-ppa')
    splitted_edge = dataset.get_edge_split()
    data = dataset[0]

    x = torch.nn.Embedding(data.num_nodes, args.hidden_channels).to(device)

    predictor = LinkPredictor(args.hidden_channels, args.hidden_channels, 1, args.num_layers,
                             args.dropout).to(device)

    evaluator = Evaluator(name='ogbl-ppa')
    loggers = {
            'Hits@10': Logger(args.runs, args),
            'Hits@50': Logger(args.runs, args),
            'Hits@100': Logger(args.runs, args),
            }

    for run in range(args.runs):
        x.reset_parameters()
        predictor.reset_parameters()
        optimizer = torch.optim.Adam(list(x.parameters()) + list(predictor.parameters()), lr=args.lr)

        for epoch in range(1, 1 + args.epochs):
            loss = train(x, predictor, splitted_edge, optimizer, args.batch_size)
            print("DONE")

            if epoch % args.eval_steps == 0:
                results = test(x, predictor, splitted_edge, evaluator, args.batch_size)
                for key, result in results.items():
                    loggers[key].add_result(run, result)

                if epoch % args.log_steps == 0:
                    for key, result in results.items():
                        train_hits, valid_hits, test_hits = result
                        print(key)
                        print(f'Run: {run + 1:02d}, '
                              f'Epoch: {epoch:02d}, '
                              f'Loss: {loss:.4f}, '
                              f'Train: {100 * train_hits:.2f}%, '
                              f'Valid: {100 * valid_hits:.2f}%, '
                              f'Test: {100 * test_hits:.2f}%')

        for key in loggers.keys():
            print(key)
            loggers[key].print_statistics(run)

    for key in loggers.keys():
        print(key)
        loggers[key].print_statistics()


if __name__ == "__main__":
    main()
