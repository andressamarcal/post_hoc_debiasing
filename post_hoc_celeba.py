import math

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision
from sklearn.metrics import accuracy_score, roc_auc_score, roc_curve
from torchvision import models, transforms

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

descriptions = ['5_o_Clock_Shadow', 'Arched_Eyebrows', 'Attractive', \
                'Bags_Under_Eyes', 'Bald', 'Bangs', 'Big_Lips', 'Big_Nose', \
                'Black_Hair', 'Blond_Hair', 'Blurry', 'Brown_Hair', \
                'Bushy_Eyebrows', 'Chubby', 'Double_Chin', 'Eyeglasses', \
                'Goatee', 'Gray_Hair', 'Heavy_Makeup', 'High_Cheekbones', \
                'Male', 'Mouth_Slightly_Open', 'Mustache', 'Narrow_Eyes', \
                'No_Beard', 'Oval_Face', 'Pale_Skin', 'Pointy_Nose', \
                'Receding_Hairline', 'Rosy_Cheeks', 'Sideburns', 'Smiling', \
                'Straight_Hair', 'Wavy_Hair', 'Wearing_Earrings', 'Wearing_Hat', \
                'Wearing_Lipstick', 'Wearing_Necklace', 'Wearing_Necktie', \
                'Young']

def load_celeba(num_workers=2, trainsize=100, testsize=100, seed=0):
    transform = transforms.ToTensor()

    trainset = torchvision.datasets.CelebA(root='./data', 
                                           download=True, 
                                           split='train', 
                                           transform=transform)

    testset = torchvision.datasets.CelebA(root='./data', 
                                          split='test',
                                          download=True, 
                                          transform=transform)

    np.random.seed(seed)

    if trainsize >= 0:
        # cut down the training set
        trainset, _ = torch.utils.data.random_split(trainset, [trainsize, len(trainset) - trainsize])
    if testsize >= 0:
        testset, _ = torch.utils.data.random_split(testset, [testsize, len(testset) - testsize])

    trainset, valset = torch.utils.data.random_split(trainset, [int(len(trainset)*0.7), int(len(trainset)*0.3)])
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=4,
                                              shuffle=True, num_workers=num_workers)
    valloader = torch.utils.data.DataLoader(valset, batch_size=4,
                                            shuffle=True, num_workers=num_workers)
    testloader = torch.utils.data.DataLoader(testset, batch_size=4,
                                             shuffle=False, num_workers=num_workers)
    return trainset, valset, testset, trainloader, valloader, testloader


class Model(nn.Module):

    def __init__(self):
        super().__init__()
        # 3 input image channels, 6 output channels, 3x3 square convolution
        # kernel
        self.conv1 = nn.Conv2d(3, 6, 3)
        self.conv2 = nn.Conv2d(6, 16, 3)
        # an affine operation: y = Wx + b
        self.fc1 = nn.Linear(36464, 1200)  # 6*6 from image dimension
        self.fc2 = nn.Linear(1200, 84)
        self.fc3 = nn.Linear(84, 1)

    def forward(self, x):
        # Max pooling over a (2, 2) window
        x = F.max_pool2d(F.relu(self.conv1(x)), (2, 2))
        # If the size is a square you can only specify a single number
        x = F.max_pool2d(F.relu(self.conv2(x)), 2)
        x = x.view(-1, self.num_flat_features(x))
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x

    def num_flat_features(self, x):
        size = x.size()[1:]  # all dimensions except the batch dimension
        num_features = 1
        for s in size:
            num_features *= s
        return num_features


def val_run(model, valloader, criterion):
    outputs = []
    valloss = 0.
    yval_trues = []
    protected = []
    with torch.no_grad():
        for valdata in valloader:
            valinputs, vallabels = valdata[0].to(device), valdata[1].to(device)
            yval_true = get_single_attr(vallabels)
            protected_label = get_single_attr(vallabels, attr='Male')
            valoutputs = model(valinputs).squeeze(-1)
            outputs.append(torch.sigmoid(valoutputs))
            valloss += criterion(valoutputs, yval_true).item()
            yval_trues.append(yval_true)
            protected.append(protected_label)
    return outputs, valloss, yval_trues, protected


def get_single_attr(labels, attr='Attractive'):

    #print(labels.shape)
    newlabels = []
    for i in range(len(labels)):
        newlabels.append(labels[i][descriptions.index(attr)])
    newlabels = torch.from_numpy(np.array(newlabels))
    newlabels = newlabels.float()
    #print(newlabels.shape)
    return newlabels


def train_model(net, trainloader, valloader, criterion, optimizer, epochs=2):
    for epoch in range(epochs):  # loop over the dataset multiple times
        running_loss = 0.0
        for i, data in enumerate(trainloader, 0):
            # get the inputs; data is a list of [inputs, labels]
            inputs, labels = data[0].to(device), data[1].to(device)
            # convert the labels into a single attribute
            ytrue = get_single_attr(labels)

            # zero the parameter gradients
            optimizer.zero_grad()

            # forward + backward + optimize
            outputs = net(inputs).squeeze(-1)
            loss = criterion(outputs, ytrue)
            loss.backward()
            optimizer.step()

            # print statistics
            running_loss += loss.item()
            if i % 2000 == 1999:
                net.eval()
                _, valloss, _, _ = val_run(net, valloader, criterion)
                print(f'[{epoch + 1},{i + 1}] trainloss: {running_loss / len(trainloader):.3f}, valloss: {valloss / len(valloader):.3f}')
                running_loss = 0.0

def compute_priors(data, protected='Male', attr='Attractive'):
    counts = np.array([[0,0],[0,0]])
    for batch in list(data):
        imgs, labels = batch[0], batch[1]

        for label in labels:
            pro_value = label[descriptions.index(protected)]
            attr_value = label[descriptions.index(attr)]
            counts[pro_value][attr_value] += 1
    total = sum(sum(counts))
    print(protected,':',np.round(sum(counts[1])/total, 4))
    print(attr,':', np.round(sum(counts[:,1])/total, 4))
    print(protected, attr, np.round(counts[1][1]/total, 4), 'Female', attr, np.round(counts[0][1]/total, 4))


def compute_bias(y_pred, y_true, priv, metric):
    def zero_if_nan(x):
        return 0. if np.isnan(x) else x

    gtpr_priv = zero_if_nan(y_pred[priv * y_true == 1].mean())
    gfpr_priv = zero_if_nan(y_pred[priv * (1-y_true) == 1].mean())
    mean_priv = zero_if_nan(y_pred[priv == 1].mean())

    gtpr_unpriv = zero_if_nan(y_pred[(1-priv) * y_true == 1].mean())
    gfpr_unpriv = zero_if_nan(y_pred[(1-priv) * (1-y_true) == 1].mean())
    mean_unpriv = zero_if_nan(y_pred[(1-priv) == 1].mean())

    if metric == "spd":
        return mean_unpriv - mean_priv
    elif metric == "aod":
        return 0.5 * ((gfpr_unpriv - gfpr_priv) + (gtpr_unpriv - gtpr_priv))
    elif metric == "eod":
        return gtpr_unpriv - gtpr_priv


def main():
    trainset, valset, tetstset, trainloader, valloader, testloader = load_celeba()
    print_priors = False
    if print_priors:
        print('train set')
        compute_priors(trainloader)
        print('val set')
        compute_priors(valloader)
        print('test set')
        compute_priors(testloader)

    net = Model()
    net.to(device)
    criterion = nn.BCEWithLogitsLoss()
    optimizer = optim.Adam(net.parameters())
    train_model(net, trainloader, valloader, criterion, optimizer)

    y_test = []
    ypred_test = []
    protected = []

    # compute test predictions/truth/protected feature
    with torch.no_grad():
        for data in testloader:
            images, labels = data[0].to(device), data[1].to(device)
            y_true = get_single_attr(labels)
            protected_label = get_single_attr(labels, attr='Male')
            outputs = net(images).squeeze(-1)
            y_test.append(y_true)
            ypred_test.append(torch.sigmoid(outputs))
            protected.append(protected_label)

    y_test = torch.cat(y_test).cpu().numpy()
    ypred_test = torch.cat(ypred_test).cpu().numpy()
    protected = torch.cat(protected).cpu().numpy()

    threshs = np.linspace(0, 1, 501)
    best_thresh = np.max([accuracy_score(y_test, ypred_test > thresh) for thresh in threshs])
    acc = accuracy_score(y_test, ypred_test > best_thresh)
    bias = compute_bias(ypred_test > best_thresh, y_test, protected, 'aod')
    obj = .75*abs(bias)+(1-.75)*(1-acc)

    print('roc auc', roc_auc_score(y_test, ypred_test))
    print('accuracy with best thresh', acc)
    print('aod', bias)
    print('objective', obj)

    print('starting random perturbation')
    rand_result = [math.inf, None, -1]
    rand_model = Model()
    for iteration in range(101):
        rand_model.load_state_dict(net.state_dict())
        rand_model.to(device)
        for param in rand_model.parameters():
            param.data = param.data * (torch.randn_like(param) * 0.1 + 1)

        rand_model.eval()
        scores, _, yval_trues, protected = val_run(rand_model, valloader, criterion)
        scores = torch.cat(scores).cpu()
        scores = scores.numpy()
        yval_trues = torch.cat(yval_trues).cpu()
        yval_trues = yval_trues.numpy()
        protected = torch.cat(protected).cpu()
        protected = protected.numpy()

        threshs = np.linspace(0, 1, 501)
        objectives = []

        for thresh in threshs:

            bias = compute_bias(scores > thresh, yval_trues, protected, 'aod')
            acc = accuracy_score(yval_trues, scores > thresh)
            objective = (0.75)*abs(bias) + (1-0.75)*(1-acc)
            objectives.append(objective)
        best_rand_thresh = threshs[np.argmax(objectives)]
        best_obj = np.max(objectives)
        if best_obj < rand_result[0]:
            del rand_result[1]
            rand_result = [best_obj, rand_model.state_dict(), best_rand_thresh]

        if iteration % 10 == 0:
            print(f"{iteration} / 101 trials have been sampled.")

    # evaluate best random model
    best_model = Model()
    best_model.load_state_dict(rand_result[1])
    best_model.to(device)
    best_thresh = rand_result[2]

    y_test = []
    ypred_test = []
    protected = []

    with torch.no_grad():
        for data in testloader:
            images, labels = data[0].to(device), data[1].to(device)
            y_true = get_single_attr(labels)
            protected_label = get_single_attr(labels, attr='Male')
            outputs = best_model(images).squeeze(-1)
            y_test.append(y_true)
            ypred_test.append(torch.sigmoid(outputs))
            protected.append(protected_label)

    y_test = torch.cat(y_test).cpu().numpy()
    ypred_test = torch.cat(ypred_test).cpu().numpy()
    protected = torch.cat(protected).cpu().numpy()

    acc = accuracy_score(y_test, ypred_test > best_thresh)
    bias = compute_bias(ypred_test > best_thresh, y_test, protected, 'aod')
    obj = .75*abs(bias)+(1-.75)*(1-acc)

    print('roc auc', roc_auc_score(y_test, ypred_test))
    print('accuracy with best thresh', acc)
    print('aod', bias)
    print('objective', obj)


if __name__ == "__main__":
    main()
