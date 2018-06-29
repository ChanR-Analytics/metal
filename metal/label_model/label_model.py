import numpy as np
from scipy.sparse import issparse, csc_matrix
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from metal.classifier import Classifier, multitask
from metal.label_model.lm_defaults import lm_model_defaults
from metal.utils import recursive_merge_dicts


class LabelModel(Classifier):
    def __init__(self, label_map=None, task_graph=None, deps=[], **kwargs):
        """
        Args:
            label_map: T-dim list of lists: The label map for each task 
                t=0,...,T-1
            task_graph: TaskGraph: A task graph...TBD
            deps: list: A list of dependencies of the form...TBD
        """
        self.config = recursive_merge_dicts(lm_model_defaults, kwargs)
        
        multitask = isinstance(label_map, list) and len(label_map) > 1
        super().__init__(multitask, self.config['seed'])

        self.label_map = label_map
        self.T = len(label_map) if label_map else 1
        
        self.task_graph = task_graph
        self.deps = deps
    
    def _infer_polarity(self, L_t, t, j):
        """Infer the polarity (labeled class) of LF j on task t"""
        assert(isinstance(L_t, csc_matrix))
        vals = set(L_t.data[L_t.indptr[j]:L_t.indptr[j+1]])
        if len(vals) > 1:
            raise Exception(f"LF {j} on task {t} is non-unipolar: {vals}.")
        elif len(vals) == 0:
            # If an LF doesn't label this task, set its polarity to 0 = abstain
            return 0
        else:
            return list(vals)[0]
    
    def _get_overlaps_matrix(self, L_t, t):
        """Initializes the data structures for training task t"""
        # Check to make sure that L_t is unipolar, and infer polarities
        p = [self._infer_polarity(L_t, t, j) for j in range(self.M)]

        # Next, we form the empirical overlaps matrix O
        # In the unipolar categorical setting, this is just the empirical count
        # of non-zero overlaps; whether these were agreements or disagreements
        # is just a function of the polarities p
        N, M = L_t.shape
        L_nz = L_t.copy()
        L_nz.data[:] = 1
        O = L_nz.T @ L_nz / N 
        O = O.todense()

        # Divide out the empirical labeling propensities
        beta = np.diag(O)
        B = np.diag(1 / np.diag(O))
        O = B @ O @ B

        # Correct the O matrix given the known polarities
        for i in range(self.M):
            for j in range(self.M):
                if i != j:
                    c = 1 if p[i] == p[j] else -1
                    O[i,j] = c * (O[i,j] - 1)

        # Converts NaNs to 0s
        # NaNs can occur when an LF abstains for all items, leaving a 0 on the
        # diagonal of the overlaps matrix which we then divide by above.
        O[np.isnan(O)] = 0

        # Turn O in PyTorch Variable
        return torch.clamp(torch.from_numpy(O), min=-0.95, max=0.95).float()
    
    def _init_params(self, gamma_init):
        """Initialize the parameters for each LF on each task separately"""
        # Note: Need to break symmetries by initializing > 0
        self.gamma_init = gamma_init
        self.gamma = nn.Parameter(gamma_init * torch.ones(self.T, self.M))
    
    def _task_loss(self, O_t, t, l2=0.0):
        """Returns the *scaled* loss (i.e. ~ loss / m^2).

        Note: A *centered* L2 loss term is incorporated here.
        The L2 term is centered around the self.gamma_init property, which thus
        also serves as the value of a prior on the gammas.
        """
        loss = 0.0
        for i in range(self.M):
            for j in range(self.M):
                if i != j:
                    loss += (self.gamma[t,i] * self.gamma[t,j] - O_t[i,j])**2

        # Normalize loss
        loss /= (self.M**2 - self.M)

        # L2 regularization, centered around gamma_init
        if l2 > 0.0:
            loss += l2 * torch.sum((self.gamma[t] - self.gamma_init)**2)
        return loss
    
    @property
    def accs(self):
        """The float *Tensor* (not Variable) of LF accuracies."""
        return torch.clamp(0.5 * (self.gamma.data + 1), min=0.01, max=0.99)
    
    @property
    def log_odds_accs(self):
        """The float *Tensor* (not Variable) of log-odds LF accuracies."""
        return torch.log(self.accs / (1 - self.accs)).float()

    @multitask([0])
    def predict_proba(self, L):
        """Get conditional probabilities P(y_t | L) given the learned LF accs

        Args:
            L: An [N, M] scipy.sparse matrix of labels or a T-length list of 
                such matrices if self.multitask=True
        Returns:
            output: An [N, K_t] tensor of soft predictions or a T-length list
                of such tensors if self.multitask=True

        Note: This implementation is for conditionally independent labeling 
        functions (given y_t); handling deps is next...
        """
        L = self._check_L(L)
        Y_ph = [self.predict_task_proba(L_t, t) for t, L_t in enumerate(L)]
        return Y_ph
    
    def predict_task_proba(self, L, t=0):
        """Get conditional probabilities P(Y_t | L) for a single task

        Args:
            L: An [N, M] scipy.sparse matrix of labels or a T-length list of 
                such matrices if self.multitask=True
            t: The task index  
        Returns:
            Y_tph: An N-dim tensor of task-specific (t), probabilistic (p), 
                hard labels (h)
        """
        L = self._check_L(L)
        # Note we cast to dense here
        L_t = torch.from_numpy(L[t].todense()).float()
        N = L_t.shape[0]

        # Here we iterate over the values of Y in {1,...,K_t}, forming
        # an [N, K_t] matrix of unnormalized predictions
        # Note in the unipolar setting:
        #   P(\lambda_j=k|y_t=k, \lambda_j != 0) = \alpha_i
        #   P(\lambda_j=k|y_t=l != k, \lambda_j != 0) = 1 - \alpha_i
        # So the computation is the same as in the binary case, except we
        # compute
        #   \theta^T \ind \{ \lambda_j != 0 \} \ind^{\pm} \{ \lambda_j = k \}
        Y_pt = torch.zeros((N, self.K_t[t]))
        for y_t in range(1, self.K_t[t] + 1):
            L_t_y = torch.where(
                (L_t != y_t) & (L_t != 0), torch.full((N, self.M), -1) , L_t)
            L_t_y = torch.where(L_t_y == y_t, torch.full((N, self.M), 1), L_t_y)
            Y_pt[:,y_t-1] = L_t_y @ self.log_odds_accs[t]

        # Take the softmax to return an [N, K_t] torch Tensor
        Y_tph = F.softmax(Y_pt, dim=1)     
        return Y_tph

    def get_accs_score(self, accs):
        """Returns the *averaged squared estimation error."""
        return np.linalg.norm(self.accs.numpy() - accs)**2 / self.M
    
    def get_loss(self, O, l2):
        """Return the loss of Y and the output(s) of the net forward pass.
        
        The returned loss is averaged over items (by the loss function) but
        summed over tasks.
        """
        loss = torch.tensor(0.0)
        for t, O_t in enumerate(O):
            loss += self._task_loss(O_t, t, l2=l2)
        return loss

    @multitask([0], ['L_dev', 'Y_dev'])
    def train(self, L_train, L_dev=None, Y_dev=None, accs=None, **kwargs):
        """Learns the accuracies of the labeling functions from L_train

        Args:
            L_train:
            L_dev:
            Y_dev:
            accs: An M-length list of the true accuracies of the LFs if known

        Note that in this class, we learn this for each task separately by
        default, and store a separate accuracy for each LF in each task.
        """
        self.config = recursive_merge_dicts(self.config, kwargs)
        train_config = self.config['train_config']
        optimizer_config = train_config['optimizer_config']

        L_train = self._check_L(L_train, init=True)

        # Get overlaps matrices for each task
        O = [self._get_overlaps_matrix(L_t, t) for t, L_t in enumerate(L_train)]

        # Init params
        self._init_params(train_config['gamma_init'])

        # Set optimizer as SGD w/ momentum
        optimizer = optim.SGD(
            self.parameters(), 
            **optimizer_config['optimizer_common'],
            **optimizer_config['sgd_config']
        )
        
        # Train model
        for epoch in range(train_config['n_epochs']):
            optimizer.zero_grad()

            # Sum over the task losses uniformly
            loss = self.get_loss(O, train_config['l2'])
            
            # Compute gradient and take a step
            # Note that since this uses all N training points this is an epoch!
            loss.backward()
            optimizer.step()
            
            # Print loss every print_at steps
            if (self.config['verbose'] and 
                (epoch % train_config['print_at'] == 0 
                or epoch == train_config['n_epochs'] - 1)):
                msg = f"[Epoch {epoch}] Loss: {loss.item():0.6f}"
                if accs is not None:
                    accs_score = self.get_accs_score(accs)
                    msg += f"\tAccs mean sq. error = {accs_score}"
                print(msg)

        if self.config['verbose']:
            print('Finished Training')

    def _check_L(self, L, init=False):
        """Check the format and content of the label tensor
        
        Args:
            L: An [N, M] scipy.sparse matrix of labels or a T-length list of 
                such matrices if self.multitask=True
            init: If True, initialize self.T, self.M, and self.label_map if 
                empty; else check against these.
        """
        # Accept single sparse matrix and make it a singleton list
        if self.multitask:
            assert(isinstance(L, list))
            assert(issparse(L[0]))
        else:
            if not isinstance(L, list):
                assert(issparse(L))
                L = [L]

        # Check or set number of tasks and labeling functions
        self._check_or_set_attr('T', len(L))
        n, m = L[0].shape
        # M should be the same for all tasks, since all LFs label all inputs
        self._check_or_set_attr('M', m, set_val=init)
        
        # Check the format and dimensions of the task label matrices
        for t, L_t in enumerate(L):
            n_t, m_t = L_t.shape
            self._check_or_set_attr('M', m_t)
            if n_t != n:
                raise Exception(f"L[{t}] has {n_t} rows, but should have {n}.")
            if not issparse(L_t):
                raise Exception(f"L[{t}] has type {type(L_t)}, but should be a"
                    "scipy.sparse matrix.")
            if L_t.dtype != np.dtype(int):
                raise Exception(f"L[{t}] has type {L_t.dtype}, should be int.")
            
            # Ensure is in CSC sparse format for efficient col (LF) slicing
            L[t] = L_t.tocsc()

        # If no label_map was provided, assume labels are continuous integers
        # starting from 1
        if self.label_map is None and init:
            if self.T > 1:
                raise Exception('Initialization parameter "label_map" cannot '
                    'be inferred when T > 1')
            K = np.amax(L[0])
            self.label_map = [list(range(K))]

        # Set cardinalities of each task
        self._check_or_set_attr('K_t', 
            [len(labels) for labels in self.label_map], set_val=init)

        # Check for consistency with cardinalities list
        for t, L_t in enumerate(L):
            if np.amax(L_t) > self.K_t[t]:
                raise Exception(f"Task {t} has cardinality {self.K_t[t]}, but"
                    "L[{t}] has max value = {np.amax(L_t)}.")
        
        return L