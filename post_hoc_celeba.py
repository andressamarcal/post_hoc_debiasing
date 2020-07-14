"""
post_hoc_celeba.py

Debias image models trained on celeba
"""
import argparse
import copy
import json
import math
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import yaml
from sklearn.metrics import roc_auc_score
from torchvision import models, transforms

from celeb_race import CelebRace, unambiguous

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
print('device:', device)
torch.manual_seed(0)

descriptions = ['5_o_Clock_Shadow', 'Arched_Eyebrows', 'Attractive',
                'Bags_Under_Eyes', 'Bald', 'Bangs', 'Big_Lips', 'Big_Nose',
                'Black_Hair', 'Blond_Hair', 'Blurry', 'Brown_Hair',
                'Bushy_Eyebrows', 'Chubby', 'Double_Chin', 'Eyeglasses',
                'Goatee', 'Gray_Hair', 'Heavy_Makeup', 'High_Cheekbones',
                'Male', 'Mouth_Slightly_Open', 'Mustache', 'Narrow_Eyes',
                'No_Beard', 'Oval_Face', 'Pale_Skin', 'Pointy_Nose',
                'Receding_Hairline', 'Rosy_Cheeks', 'Sideburns', 'Smiling',
                'Straight_Hair', 'Wavy_Hair', 'Wearing_Earrings', 'Wearing_Hat',
                'Wearing_Lipstick', 'Wearing_Necklace', 'Wearing_Necktie',
                'Young', 'White', 'Black', 'Asian', 'Index', 'Female']


def load_celeba(input_size=224, num_workers=2, trainsize=100, testsize=100, batch_size=4, transform_type='normalize'):
    """Load CelebA dataset"""

    if transform_type == 'normalize':
        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ])
    elif transform_type == 'augmentation':
        transform = transforms.Compose([
            transforms.RandomResizedCrop(input_size),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ])
    else:
        transform = transforms.ToTensor()

    trainset = CelebRace(root='./data', download=True, split='train', transform=transform)
    testset = CelebRace(root='./data', download=True, split='test', transform=transform)

    # return only the images which were predicted white, black, or asian by >70%.
    trainset = unambiguous(trainset, split='train')
    testset = unambiguous(testset, split='test')

    if trainsize >= 0:
        # cut down the training set
        trainset, _ = torch.utils.data.random_split(trainset, [trainsize, len(trainset) - trainsize])
    trainset, valset = torch.utils.data.random_split(trainset, [int(len(trainset)*0.7), int(len(trainset)*0.3)])
    if testsize >= 0:
        testset, _ = torch.utils.data.random_split(testset, [testsize, len(testset) - testsize])

    trainloader = torch.utils.data.DataLoader(trainset, batch_size=batch_size, shuffle=False, num_workers=num_workers)
    valloader = torch.utils.data.DataLoader(valset, batch_size=batch_size, shuffle=False, num_workers=num_workers)
    testloader = torch.utils.data.DataLoader(testset, batch_size=batch_size, shuffle=False, num_workers=num_workers)

    return trainset, valset, testset, trainloader, valloader, testloader


def get_resnet_model():
    """Get Pretrained resnet model"""
    resnet18 = models.resnet18(pretrained=True)
    num_ftrs = resnet18.fc.in_features
    resnet18.fc = nn.Linear(num_ftrs, 2)
    resnet18.to(device)
    return resnet18


def get_best_accuracy(y_true, y_pred, _):
    """Select threshold that maximizes accuracy"""
    threshs = torch.linspace(0, 1, 1001)
    best_acc, best_thresh = 0., 0.
    for thresh in threshs:
        acc = torch.mean(((y_pred > thresh) == y_true).float()).item()
        if acc > best_acc:
            best_acc, best_thresh = acc, thresh
    return best_acc, best_thresh


def train_model(model, trainloader, valloader, criterion, optimizer, checkpoint, protected_index, prediction_index, epochs=2, start_epoch=0):
    """Fine-tune resnet model on dataset"""
    best_acc, best_model, patience = 0., None, 10
    for epoch in range(start_epoch, epochs):
        print('Epoch {}/{}'.format(epoch+1, epochs))
        print('-' * 10)

        model.train()

        running_loss = 0.
        running_corrects = 0

        for index, (inputs, labels) in enumerate(trainloader):
            inputs, labels = inputs.to(device), (labels[:, prediction_index]).float().to(device)

            optimizer.zero_grad()

            outputs = model(inputs)
            loss = criterion(outputs[:, 0], labels)

            preds = torch.sigmoid(outputs[:, 0]) > 0.5

            loss.backward()
            optimizer.step()

            running_loss += loss.item() * inputs.size(0)
            running_corrects += torch.sum(preds == labels.data)

            if (index-1) % 101 == 0:
                num_examples = index * inputs.size(0)
                print(f"({index}/{len(trainloader)}) Loss: {running_loss / num_examples:.4f} Acc: {running_corrects.float() / num_examples:.4f}")

        acc, _ = val_model(model, valloader, get_best_accuracy, protected_index, prediction_index)
        if acc < best_acc:
            patience -= 1
            if patience <= 0:
                model.load_state_dict(best_model)
        else:
            best_acc = acc
            best_model = model.state_dict()
            patience = 10
        print(f"Best Accuracy on Validation set: {best_acc}")
        torch.save({
            'epoch': epoch,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
        }, checkpoint)
        if patience <= 0:
            break


def val_model(model, loader, criterion, protected_index, prediction_index):
    """Validate model on loader with criterion function"""
    y_true, y_pred, y_prot = [], [], []
    model.eval()
    with torch.no_grad():

        for inputs, full_labels in loader:
            inputs, labels, protected = inputs.to(device), full_labels[:, prediction_index].float().to(device), full_labels[:, protected_index].float().to(device)
            y_true.append(labels)
            y_prot.append(protected)
            y_pred.append(torch.sigmoid(model(inputs)[:, 0]))
    y_true, y_pred, y_prot = torch.cat(y_true), torch.cat(y_pred), torch.cat(y_prot)
    return criterion(y_true, y_pred, y_prot)


def compute_priors(data, protected_index, prediction_index):
    """Compute priors on the data"""
    counts = np.zeros((2, 2))
    for batch in list(data):
        _, labels = batch[0], batch[1]

        for label in labels:
            prot_value = label[protected_index]
            pred_value = label[prediction_index]
            counts[prot_value][pred_value] += 1
    total = sum(sum(counts))

    prot_rate = np.round(counts[1][1]/sum(counts[1]), 4)
    unprot_rate = np.round(counts[0][1]/sum(counts[0]), 4)

    print('Prob. protected class:', np.round(sum(counts[1])/total, 4))
    print('Prob. positive outcome:', np.round(sum(counts[:, 1])/total, 4))
    print('Prob. positive outcome given protected class', prot_rate)
    print('Prob. positive outcome given unprotected class', unprot_rate)


def compute_bias(y_pred, y_true, prot, metric):
    """Compute bias on the dataset"""
    def zero_if_nan(data):
        """Zero if there is a nan"""
        return 0. if torch.isnan(data) else data

    gtpr_prot = zero_if_nan(y_pred[prot * y_true == 1].mean())
    gfpr_prot = zero_if_nan(y_pred[prot * (1-y_true) == 1].mean())
    mean_prot = zero_if_nan(y_pred[prot == 1].mean())

    gtpr_unprot = zero_if_nan(y_pred[(1-prot) * y_true == 1].mean())
    gfpr_unprot = zero_if_nan(y_pred[(1-prot) * (1-y_true) == 1].mean())
    mean_unprot = zero_if_nan(y_pred[(1-prot) == 1].mean())

    if metric == "spd":
        return mean_prot - mean_unprot
    elif metric == "aod":
        return 0.5 * ((gfpr_prot - gfpr_unprot) + (gtpr_prot - gtpr_unprot))
    elif metric == "eod":
        return gtpr_prot - gtpr_unprot


def get_objective_with_best_accuracy(y_true, y_pred, y_prot):
    """Get objective for best accuracy threshold"""
    rocauc_score = roc_auc_score(y_true.cpu(), y_pred.cpu())
    best_acc, best_thresh = get_best_accuracy(y_true, y_pred, y_prot)
    bias = compute_bias((y_pred > best_thresh).float().cpu(), y_true.float().cpu(), y_prot.float().cpu(), 'aod')
    obj = .75*abs(bias)+(1-.75)*(1-best_acc)
    return rocauc_score, best_acc, bias, obj


def get_best_objective(y_true, y_pred, y_prot):
    """Find the threshold for the best objective"""
    threshs = torch.linspace(0, 1, 501)
    best_obj, best_thresh = math.inf, 0.
    for thresh in threshs:
        acc = torch.mean(((y_pred > thresh) == y_true).float()).item()
        bias = compute_bias((y_pred > thresh).float().cpu(), y_true.float().cpu(), y_prot.float().cpu(), 'aod')
        obj = .75*abs(bias)+(1-.75)*(1-acc)
        if obj < best_obj:
            best_obj, best_thresh = obj, thresh

    return best_obj, best_thresh


def get_objective_results(best_thresh):
    """Get the objective results with the best_threshold"""
    def _get_results(y_true, y_pred, y_prot):
        """Inner function to be returned"""
        rocauc_score = roc_auc_score(y_true.cpu(), y_pred.cpu())
        acc = torch.mean(((y_pred > best_thresh) == y_true).float()).item()
        bias = compute_bias((y_pred > best_thresh).float().cpu(), y_true.float().cpu(), y_prot.float().cpu(), 'aod')
        obj = .75*abs(bias)+(1-.75)*(1-acc)

        return rocauc_score, acc, bias, obj
    return _get_results


def print_objective_results(dataloader, model, thresh, protected_index, prediction_index):

    rocauc_score, acc, bias, obj = val_model(model, dataloader, get_objective_results(thresh), protected_index, prediction_index)

    print('roc auc', rocauc_score)
    print('accuracy with best thresh', acc)
    print('aod', bias.item())
    print('objective', obj.item())

    result_dict = {
        'roc_auc': float(rocauc_score),
        'accuracy': float(acc),
        'bias': float(bias.item()),
        'objective': float(obj.item())
    }

    return result_dict

class Critic(nn.Module):
    """Critic class for adversarial debiasing method"""

    def __init__(self, sizein, num_deep=3, hid=32):
        super().__init__()
        self.fc0 = nn.Linear(sizein, hid)
        self.fcs = nn.ModuleList([nn.Linear(hid, hid) for _ in range(num_deep)])
        self.dropout = nn.Dropout(0.2)
        self.out = nn.Linear(hid, 1)

    def forward(self, t):
        t = t.reshape(1, -1)
        t = self.fc0(t)
        for fully_connected in self.fcs:
            t = F.relu(fully_connected(t))
            t = self.dropout(t)
        return self.out(t)


def main(config):
    """Main Function"""
    protected_index = descriptions.index(config['protected_attr'])
    prediction_index = descriptions.index(config['prediction_attr'])
    results = {}

    _, _, _, trainloader, valloader, testloader = load_celeba(
        trainsize=config['trainsize'],
        testsize=config['testsize'],
        num_workers=config['num_workers'],
        batch_size=config['batch_size']
    )
    if config['print_priors']:
        print('train priors')
        compute_priors(trainloader, protected_index, prediction_index)
        print()
        print('val priors')
        compute_priors(valloader, protected_index, prediction_index)
        print()
        print('test priors')
        compute_priors(testloader, protected_index, prediction_index)
        print()

    net = get_resnet_model()
    criterion = nn.BCEWithLogitsLoss()
    optimizer = optim.Adam(net.parameters())
    checkpoint_file = Path(config['checkpoint'])

    start_epoch = 0
    if checkpoint_file.is_file():
        checkpoint = torch.load(checkpoint_file)
        print('loaded from', checkpoint_file)
        net.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch']
    if config['retrain']:
        train_model(
            net,
            trainloader,
            valloader,
            criterion,
            optimizer,
            config['checkpoint'],
            protected_index,
            prediction_index,
            epochs=config['epochs'],
            start_epoch=start_epoch
        )

    _, best_thresh = val_model(net, valloader, get_best_accuracy, protected_index, prediction_index)

    print('val_results, thresh', best_thresh.item())
    print_objective_results(valloader, net, best_thresh, protected_index, prediction_index)
    print()
    print('test_results')
    result_dict = print_objective_results(testloader, net, best_thresh, protected_index, prediction_index)
    print()
    results['base_model'] = result_dict

    if 'random' in config['models']:

        best_obj, best_thresh = val_model(net, valloader, get_best_objective, protected_index, prediction_index)
        print('val best thresh results, thresh', best_thresh.item())
        print_objective_results(valloader, net, best_thresh, protected_index, prediction_index)
        print()
        print('test best thresh results')
        result_dict = print_objective_results(testloader, net, best_thresh, protected_index, prediction_index)
        print()

        rand_result = [math.inf, None, -1]

        for iteration in range(101):
            rand_model = copy.deepcopy(net)
            rand_model.to(device)
            for param in rand_model.parameters():
                param.data = param.data * (torch.randn_like(param) * 0.1 + 1)

            rand_model.eval()
            best_obj, best_thresh = val_model(rand_model, valloader, get_best_objective, protected_index, prediction_index)
            print('iteration', iteration, 'obj', best_obj.item(), 'thresh', best_thresh.item())

            if best_obj < rand_result[0]:
                print('found new best')
                del rand_result[1]
                rand_result = [best_obj, rand_model.state_dict(), best_thresh]

            if iteration % 10 == 0:
                print(f"{iteration} / 101 trials have been sampled.")
                print('current best obj', rand_result[0].item())

        # evaluate best random model
        best_model = copy.deepcopy(net)
        best_model.load_state_dict(rand_result[1])
        best_model.to(device)
        best_thresh = rand_result[2]

        print('val_results')
        print_objective_results(valloader, best_model, best_thresh, protected_index, prediction_index)
        print()
        print('test_results')
        result_dict = print_objective_results(testloader, best_model, best_thresh, protected_index, prediction_index)
        print()
        results['random'] = result_dict

        torch.save(best_model.state_dict(), config['random']['checkpoint'])

    if 'adversarial' in config['models']:
        base_model = copy.deepcopy(net)
        base_model.fc = nn.Linear(base_model.fc.in_features, base_model.fc.in_features)

        actor = nn.Sequential(base_model, nn.Linear(base_model.fc.in_features, 2))
        actor.to(device)
        actor_optimizer = optim.Adam(actor.parameters())
        actor_loss_fn = nn.BCEWithLogitsLoss()
        actor_loss = 0.
        actor_steps = config['adversarial']['actor_steps']

        critic = Critic(config['batch_size']*net.fc.in_features)
        critic.to(device)
        critic_optimizer = optim.Adam(critic.parameters())
        critic_loss_fn = nn.MSELoss()
        critic_loss = 0.
        critic_steps = config['adversarial']['critic_steps']

        for epoch in range(config['adversarial']['epochs']):
            for param in critic.parameters():
                param.requires_grad = True
            for param in actor.parameters():
                param.requires_grad = False
            actor.eval()
            critic.train()
            for step, (inputs, labels) in enumerate(valloader):
                if step > critic_steps:
                    break
                inputs, labels = inputs.to(device), labels.to(device)
                if inputs.size(0) != config['batch_size']:
                    continue
                critic_optimizer.zero_grad()

                with torch.no_grad():
                    y_pred = actor(inputs)

                y_true = labels[:, prediction_index].float().to(device)
                y_prot = labels[:, protected_index].float().to(device)

                bias = compute_bias(y_pred, y_true, y_prot, 'aod')
                res = critic(base_model(inputs))
                loss = critic_loss_fn(bias.unsqueeze(0), res[0])
                loss.backward()
                critic_loss += loss.item()
                critic_optimizer.step()
                if step % 100 == 0:
                    print_loss = critic_loss if (epoch*critic_steps + step) == 0 else critic_loss / (epoch*critic_steps + step)
                    print(f'=======> Epoch: {(epoch, step)} Critic loss: {print_loss:.3f}')

            for param in critic.parameters():
                param.requires_grad = False
            for param in actor.parameters():
                param.requires_grad = True
            actor.train()
            critic.eval()
            for step, (inputs, labels) in enumerate(valloader):
                if step > actor_steps:
                    break
                inputs, labels = inputs.to(device), labels.to(device)
                if inputs.size(0) != config['batch_size']:
                    continue
                actor_optimizer.zero_grad()

                y_true = labels[:, prediction_index].float().to(device)
                y_prot = labels[:, protected_index].float().to(device)

                lam = config['adversarial']['lambda']

                est_bias = critic(base_model(inputs))
                loss = actor_loss_fn(actor(inputs)[:, 0], y_true)
                loss = lam*abs(est_bias) + (1-lam)*loss

                loss.backward()
                actor_loss += loss.item()
                actor_optimizer.step()
                if step % 100 == 0:
                    print_loss = critic_loss if (epoch*actor_steps + step) == 0 else critic_loss / (epoch*actor_steps + step)
                    print(f'=======> Epoch: {(epoch, step)} Actor loss: {print_loss:.3f}')

        _, best_thresh = val_model(actor, valloader, get_best_objective, protected_index, prediction_index)

        print('val_results')
        print_objective_results(valloader, actor, best_thresh, protected_index, prediction_index)
        print()
        print('test_results')
        result_dict = print_objective_results(testloader, actor, best_thresh, protected_index, prediction_index)
        print()
        results['adversarial'] = result_dict

        torch.save(actor.state_dict(), config['adversarial']['checkpoint'])

    with open(config['output'], 'w') as filehandler:
        json.dump(results, filehandler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Args for CelebA experiments')
    parser.add_argument("config", help="Path to configuration yaml file.")
    args = parser.parse_args()
    with open(args.config, 'r') as fh:
        yaml_config = yaml.load(fh, Loader=yaml.FullLoader)
    main(yaml_config)
