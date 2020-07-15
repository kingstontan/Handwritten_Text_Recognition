import torch as torch
import os
import shutil
import network
from params import *
import data_utils
from torch.nn import CTCLoss
import torch.nn as nn
from myDataset import myDataset
from torch.utils.data import DataLoader
import torch.optim as optim
from torch.autograd import Variable
from tqdm import tqdm
from tensorboardX import SummaryWriter
from utils import CER, WER

# ------------------------------------------------
"""
In this block
    Set path to log
"""
# os.environ['CUDA_VISIBLE_DEVICES'] = '2,3'

params, log_dir = BaseOptions().parser()
print("log_dir =", log_dir)
if params.save:
    writer = SummaryWriter(log_dir)  # TensorBoard(log_dir)
else:
    shutil.rmtree(log_dir)

# -----------------------------------------------
"""
In this block
    Net init
    Weight init
    Load pretrained model
"""


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


def net_init():
    nclass = len(alphabet)
    rcnn = network.RCNN(imheight=params.imgH,
                        nc=params.NC,
                        n_conv_layers=params.N_CONV_LAYERS,
                        n_conv_out=params.N_CONV_OUT,
                        conv=params.CONV,
                        batch_norm=params.BATCH_NORM,
                        max_pool=params.MAX_POOL,
                        n_r_layers=params.N_REC_LAYERS,
                        n_hidden=params.N_HIDDEN,
                        n_out=params.N_CHARACTERS,
                        bidirectional=params.BIDIRECTIONAL)

    if params.pretrained != '':
        print('Loading pretrained model from %s' % params.pretrained)
        # if params.multi_gpu:
        #    rcnn = torch.nn.DataParallel(rcnn)
        rcnn.load_state_dict(torch.load(params.pretrained))
        print('Loading done.')
    elif params.weights_init:
        rcnn.apply(weights_init)

    return rcnn

# -----------------------------------------------
"""
In this block
    training function 
    evaluation function
"""

def test(model, criterion, test_loader, batch_size):
    print("Starting testing...")
    model.eval()

    avg_cost = 0
    avg_CER = 0
    avg_WER = 0
    for iter_idx, (img, transcr) in enumerate(tqdm(test_loader)):
        # Process predictions
        img = Variable(img.data.unsqueeze(1))
        if params.cuda and torch.cuda.is_available():
            img = img.cuda()
        # print(img.type)
        with torch.no_grad():
            preds = model(img)
        preds_size = Variable(torch.LongTensor([preds.size(0)] * batch_size))

        # Process labels for CTCLoss
        labels = Variable(torch.LongTensor([cdict[c] for c in ''.join(transcr)]))
        label_lengths = torch.LongTensor([len(t) for t in transcr])
        # Compute CTCLoss
        if params.cuda and torch.cuda.is_available():
            preds_size = preds_size.cuda()
            labels = labels.cuda()
            label_lengths = label_lengths.cuda()
        cost = criterion(preds, labels, preds_size, label_lengths)  # / batch_size
        avg_cost += cost.item()

        # Convert paths to string for metrics
        tdec = preds.argmax(2).permute(1, 0).cpu().numpy().squeeze()
        for k in range(len(tdec)):
            tt = [v for j, v in enumerate(tdec[k]) if j == 0 or v != tdec[k][j - 1]]
            dec_transcr = ''.join([icdict[t] for t in tt]).replace('_', '')
            # Compute metrics
            avg_CER += CER(transcr[k], dec_transcr)
            avg_WER += WER(transcr[k], dec_transcr)
            if iter_idx % 50 == 0 and k % 2 == 0:
                print('label:', transcr[k])
                print('prediction:', dec_transcr)
                print('CER:', CER(transcr[k], dec_transcr))
                print('WER:', WER(transcr[k], dec_transcr))
                if params.save:
                    writer.add_text(transcr[k],
                                    dec_transcr + '  --[CER=' + str(
                                        round(CER(transcr[k], dec_transcr), 2)) + ']', 0)
                    writer.add_text(transcr[k],
                                    dec_transcr + '  --[WER=' + str(
                                        round(WER(transcr[k], dec_transcr), 2)) + ']', 0)

    avg_cost = avg_cost / len(test_loader)
    avg_CER = avg_CER / (len(test_loader) * batch_size)
    avg_WER = avg_WER / (len(test_loader) * batch_size)
    print('Average CTCloss', avg_cost)
    print("Average CER", avg_CER)
    print("Average WER", avg_WER)

    print("Testing done.")
    return avg_cost, avg_CER, avg_WER


def val(model, criterion, val_loader):
    model.eval()
    avg_cost = 0

    for iter_idx, (img, transcr) in enumerate(tqdm(val_loader)):
        # Process predictions
        img = Variable(img.data.unsqueeze(1))
        if params.cuda and torch.cuda.is_available():
            img = img.cuda()
        # print(img.type)
        with torch.no_grad():
            preds = model(img)
        preds_size = Variable(torch.LongTensor([preds.size(0)] * img.size(0)))

        # Process labels for CTCLoss
        labels = Variable(torch.LongTensor([cdict[c] for c in ''.join(transcr)]))
        label_lengths = torch.LongTensor([len(t) for t in transcr])
        # Compute CTCLoss
        if params.cuda and torch.cuda.is_available():
            preds_size = preds_size.cuda()
            labels = labels.cuda()
            label_lengths = label_lengths.cuda()
        cost = criterion(preds, labels, preds_size, label_lengths)  # / batch_size
        avg_cost += cost.item()

    avg_cost = avg_cost / len(val_loader)
    return(avg_cost)


def train(model, criterion, optimizer, train_loader, val_loader):
    print("Starting training...")
    losses = []

    optimizer.zero_grad()

    for epoch in range(params.epochs):
        # Training
        for p in model.parameters():
            p.requires_grad = True
        model.train()
        avg_cost = 0
        for iter_idx, (img, transcr) in enumerate(tqdm(train_loader)):
            # Process predictions
            img = Variable(img.data.unsqueeze(1))
            if params.cuda and torch.cuda.is_available():
                img = img.cuda()
            preds = model(img)
            preds_size = Variable(torch.LongTensor([preds.size(0)] * img.size(0)))
            # Process labels
            # CTCLoss().cuda() only works with LongTensor
            labels = Variable(torch.LongTensor([cdict[c] for c in ''.join(transcr)]))
            label_lengths = torch.LongTensor([len(t) for t in transcr])
            # criterion = CTC loss
            if params.cuda and torch.cuda.is_available():
                preds_size = preds_size.cuda()
                labels = labels.cuda()
                label_lengths = label_lengths.cuda()
            cost = criterion(preds, labels, preds_size, label_lengths)# / batch_size
            avg_cost += cost.item()
            cost.backward()
            optimizer.step()
            # del preds_size, labels, label_lengths, cost
            # del img, preds, preds_size, labels, label_lengths, cost
        avg_cost = avg_cost/len(train_loader)

        # log the loss
        if params.save:
            writer.add_scalar('train loss', avg_cost, epoch)
        # Convert paths to string for metrics
        tdec = preds.argmax(2).permute(1, 0).cpu().numpy().squeeze()
        tt = [v for j, v in enumerate(tdec[0]) if j == 0 or v != tdec[0][j - 1]]

        if params.save:
            dec_transcr = 'Train epoch ' + str(epoch).zfill(4) + ' Prediction ' + ''.join(
                [icdict[t] for t in tt]).replace('_', '')
            writer.add_image(dec_transcr, img[0], epoch)

        # Validation
        val_loss =val(model, criterion, val_loader)
        if params.save:
            writer.add_scalar('val loss', val_loss, epoch)

        losses.append(avg_cost)
        print("img = ", img.shape)
        # print("preds = ", preds)
        # print("labels = ", labels)
        #print("preds_size = ", preds_size)
        print("label_lengths = ", label_lengths)
        print('Epoch[%d/%d] \n Average Training Loss: %f \n Average validation loss: %f' % (epoch+1, params.epochs, avg_cost, val_loss))

    print("Training done.")
    return losses



# -----------------------------------------------
"""
In this block
    criterion define
"""
CRITERION = CTCLoss()
if params.cuda and torch.cuda.is_available():
    CRITERION = CRITERION.cuda()

# -----------------------------------------------

if __name__ == "__main__":
    torch.cuda.empty_cache()
    # Initialize model
    MODEL = net_init()
    # print(MODEL)
    if params.cuda and torch.cuda.is_available():
        MODEL = MODEL.cuda()

    # Initialize optimizer
    if params.adam:
        OPTIMIZER = optim.Adam(MODEL.parameters(), lr=params.lr, betas=(params.beta1, 0.999))
    else:
        OPTIMIZER = optim.RMSprop(MODEL.parameters(), lr=params.lr)

    # Load data
    # when data_size = (32, None), the width is not fixed
    train_set = myDataset(data_size=(params.imgH, params.imgW), set='train')
    test_set = myDataset(data_size=(params.imgH, params.imgW), set='test')
    val1_set = myDataset(data_size=(params.imgH, params.imgW), set='val1')
    print("len(train_set) =", train_set.__len__())
    print("len(test_set) =", test_set.__len__())
    print("len(val1_set) =", val1_set.__len__())

    # augmentation using data sampler
    TRAIN_LOADER = DataLoader(train_set, batch_size=params.batch_size, shuffle=True, num_workers=8,
                              collate_fn=data_utils.pad_packed_collate)
    TEST_LOADER = DataLoader(test_set, batch_size=params.batch_size, shuffle=False, num_workers=8,
                             collate_fn=data_utils.pad_packed_collate)
    VAL_LOADER = DataLoader(val1_set, batch_size=params.batch_size, shuffle=True, num_workers=8,
                            collate_fn=data_utils.pad_packed_collate)
    # Train model
    #train(MODEL, CRITERION, OPTIMIZER, TRAIN_LOADER, VAL_LOADER)

    # eventually save model
    if params.save:
        torch.save(MODEL.state_dict(), '{0}/netRCNN.pth'.format(log_dir))
        print("Network saved at location %s" % log_dir)

    # Test model
    test(MODEL, CRITERION, TEST_LOADER, params.batch_size)



    del MODEL
