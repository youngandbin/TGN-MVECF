import math, time, argparse, gc, torch, pickle, wandb, numpy as np
from pathlib import Path
from tqdm import tqdm
from model.tgn import TGN
from evaluation import eval_recommendation
from utils.data import get_data, compute_time_statistics
from utils.utils import EarlyStopMonitor, RandEdgeSampler, get_neighbor_finder
torch.manual_seed(0)
np.random.seed(0)

import scipy.stats as stats

def new_rank_of_weighted_ranks(list1, list2, weight1): 
  
    weight2 = 1 - weight1
    # Ensure the sum of weights is 1
    assert weight1 + weight2 == 1, "The sum of weights must be 1"
    
    # Rank the elements in each list
    ranks1 = stats.rankdata(list1)
    ranks2 = stats.rankdata(list2)
    
    # Calculate the weighted average rank for each element
    weighted_ranks = [weight1 * rank1 + weight2 * rank2 for rank1, rank2 in zip(ranks1, ranks2)]
    
    # Rank the elements based on their weighted average ranks
    final_ranks = stats.rankdata(weighted_ranks)
    
    return final_ranks
  
"""
argument
"""
parser = argparse.ArgumentParser('Stock Rec')
# setting
parser.add_argument('--wandb_name', type=str, default='stock_rec', help='Name of the wandb project')
parser.add_argument('--gpu', type=int, default=0, help='Idx for the gpu to use')
parser.add_argument('--prefix', type=str, default='', help='Prefix to name the checkpoints')
parser.add_argument('--test_run', action='store_true', help='*run only first two batches')
# model
parser.add_argument('--memory_dim', type=int, default=64, help='Dimensions of the memory for each user')
parser.add_argument('--model_name', type=str, default="ours", choices=["ours", "tgn", "tgat", "jodie", "dyrep"], help='Type of model')
parser.add_argument('--loss_alpha', type=float, default=0.5, help='weight on contrastive loss')
parser.add_argument('--n_heads', type=int, default=2, help='Number of attention heads') 
parser.add_argument('--lr', type=float, default=0.0001, help='Learning rate') 
# training
parser.add_argument('--epoch', type=int, default=1, help='Number of epochs')
parser.add_argument('--period', type=str, default='1', help='Period of data to use (1 to 7)')
parser.add_argument('--bs', type=int, default=512, help='Batch_size')
parser.add_argument('--drop_out', type=float, default=0.1, help='Dropout probability')
parser.add_argument('--num_negatives', type=int, default=10, help='*part of batch items')
# parser.add_argument('--num_neg_train', type=int, default=3, help='*p_pos and p_neg items')
# evaluation
parser.add_argument('--num_neg_eval', type=int, default=50, help='*neg items for evaluation')
# parser.add_argument('--num_rec', type=int, default=1, help='*top k items for evaluation')
# mvecf
parser.add_argument('--cui', type=float, default=1)
parser.add_argument('--gamma', type=float, default=2)
parser.add_argument('--lambda_mv', type=float, default=1e-8)

parser.add_argument('--p_pos_num', type=int, default=1, help='Number of positive item in mvecf')
parser.add_argument('--p_neg_num', type=int, default=3, help='Number of negative item in mvecf')
args = parser.parse_args()

WANDB_NAME = args.wandb_name
wandb.init(project=f"{WANDB_NAME}", config=args, name=args.prefix)

"""
global variables
"""
NUM_EPOCH = args.epoch
USE_MEMORY = True
BACKPROP_EVERY = 1
N_LAYERS = 1
N_HEADS = args.n_heads
LEARNING_RATE = args.lr
args.memory_updater = 'gru' # choices=["gru", "rnn"]
args.embedding_module = 'graph_attention' # choices=["graph_attention", "graph_sum", "identity", "time"]
args.dyrep = False
args.use_destination_embedding_in_message = False
args.n_degree = 10 # Number of neighbors to sample
args.uniform = False # uniform: take uniform sampling from temporal neighbors (else: most recent neighbors)
# baseline models
if args.model_name=='jodie':
  args.memory_updater = 'rnn'
  args.embedding_module = 'time'
elif args.model_name=='dyrep':
  args.memory_updater = 'rnn'
  args.use_destination_embedding_in_message = True
  args.dyrep = True
elif args.model_name=='tgat':
  USE_MEMORY = False
  N_HEADS = args.n_heads
  LEARNING_RATE = args.lr
  args.uniform = True
  


"""
save paths
"""
# Path(f"results/{args.prefix}").mkdir(parents=True, exist_ok=True) # save results from valid, test data
# Path(f"saved/{args.prefix}").mkdir(parents=True, exist_ok=True) # save model checkpoints 
# get_checkpoint_path = lambda epoch: f'./saved/{args.prefix}/{epoch}.pth'

"""
data
""" 
node_features, edge_features, full_data, train_data, val_data, test_data, upper_u = get_data('transaction', args.period)
node_features = np.random.rand(len(node_features), args.memory_dim)                  # Generate new node features randomly based on the memory dimension (memory dim).
time_feature = pickle.load(open(f'data/time_feature_future.pkl', 'rb'))                # Dictionary containing historical daily prices for all stocks for each timestamp (ts).
map_item_id = pickle.load(open(f'data/period_{args.period}/map_item_id.pkl', 'rb'))  # Used to convert stock codes in a user portfolio to item idx.

"""
init
"""
# Initialize neighbor finder to retrieve temporal graph
train_ngh_finder = get_neighbor_finder(train_data, uniform=args.uniform) 
full_ngh_finder = get_neighbor_finder(full_data, uniform=args.uniform)

# Compute time statistics
mean_time_shift_src, std_time_shift_src, mean_time_shift_dst, std_time_shift_dst = \
  compute_time_statistics(full_data.sources, full_data.destinations, full_data.timestamps)

# Set device
device = torch.device('cuda:{}'.format(args.gpu))

# Initialize Model
tgn = TGN(neighbor_finder=train_ngh_finder, node_features=node_features,
          edge_features=edge_features, device=device,
          n_layers=N_LAYERS,
          n_heads=N_HEADS, dropout=args.drop_out, use_memory=USE_MEMORY,
          message_dimension=100, memory_dimension=args.memory_dim,
          memory_update_at_start=True,
          embedding_module_type=args.embedding_module,
          message_function='identity',
          aggregator_type='last',
          memory_updater_type=args.memory_updater,
          n_neighbors=args.n_degree,
          mean_time_shift_src=mean_time_shift_src, std_time_shift_src=std_time_shift_src,
          mean_time_shift_dst=mean_time_shift_dst, std_time_shift_dst=std_time_shift_dst,
          use_destination_embedding_in_message=args.use_destination_embedding_in_message,
          use_source_embedding_in_message=False,
          dyrep=args.dyrep)

optimizer = torch.optim.Adam(tgn.parameters(), lr=LEARNING_RATE)
tgn = tgn.to(device)

# # Load pretrained model if specified
# if args.load_model is not None:
#   load_path = sorted(Path(f'saved/{args.load_model}').glob('*.pth'))[-2]
#   tgn.load_state_dict(torch.load(load_path))
#   print(f'Loaded model from {load_path}')

num_instance = len(train_data.sources)
num_batch = math.ceil(num_instance / args.bs)

gc.collect() # These commands help you when you face CUDA OOM error
torch.cuda.empty_cache()

best_val_score = 0

for epoch in tqdm(range(NUM_EPOCH), desc="Progress: Epoch Loop" ):  
  time_start = time.time()
  
  """
  Train=======================================================================================================================================
  """

  # Reinitialize memory of the model at the start of each epoch
  if USE_MEMORY:
    tgn.memory.__init_memory__()

  # Train using only training graph
  tgn.set_neighbor_finder(train_ngh_finder)

  losses_batch = []
  y_mv_pos, y_tilde_pos = [], []
  y_mv_neg, y_tilde_neg = [], []
  c_mv_pos, c_mv_neg = [], []
  original_ranking_list = []
  change_ranking_list = []
  pos_to_neg_list = []
  invest_neg_to_neg_list = []

    
  for batch in tqdm(range(0, num_batch, BACKPROP_EVERY), total=num_batch//BACKPROP_EVERY, desc="Progress: Train Batch Loop"):

    # test run
    if args.test_run:
      if batch == 2:
        break

    loss = 0
    optimizer.zero_grad()

    # Custom loop to allow to perform backpropagation only every a certain number of batches
    for j in range(BACKPROP_EVERY):
    

      batch_idx = batch + j

      if batch_idx >= num_batch:
        continue

      s_idx = batch_idx * args.bs
      e_idx = min(num_instance, s_idx + args.bs)
      
      # batch data 뽑기: <class 'numpy.ndarray'>
      sources_batch = train_data.sources[s_idx:e_idx]           # (BATCH_SIZE,) 
      destinations_batch = train_data.destinations[s_idx:e_idx] # (BATCH_SIZE,) # item idx
      edge_idxs_batch  = train_data.edge_idxs[s_idx: e_idx]     # (BATCH_SIZE,)
      timestamps_batch = train_data.timestamps[s_idx:e_idx]     # (BATCH_SIZE,)
      portfolios_batch = train_data.portfolios[s_idx:e_idx]     # (BATCH_SIZE,) # stock code (6 digits)
      
      # calculate embeddings and loss
      if args.model_name == 'ours': 
        
        # 240601 # 샘플링을 배치 내에서 하지 않고 전체에서 함
        destinations = train_data.destinations 

        # negative sampling
        train_rand_sampler = RandEdgeSampler(sources_batch, destinations, portfolios_batch, upper_u, map_item_id)
        negatives_batch = train_rand_sampler.sample(size=args.num_negatives)  # (BATCH_SIZE, size) # item idx
        # Convert item idx to stock codes 
        destinations_batch = destinations_batch - (upper_u + 1)                                           # item idx -> Index starting from 0
        destinations_batch = np.vectorize({v: k for k, v in map_item_id.items()}.get)(destinations_batch) # Index starting from 0 -> stock code (6 digits)
        negatives_batch = negatives_batch - (upper_u + 1)                                                 # item idx -> Index starting from 0
        negatives_batch = np.vectorize({v: k for k, v in map_item_id.items()}.get)(negatives_batch)       # Index starting from 0 -> stock code (6 digits)
        # candidates = positive + negatives
        candidates_batch = np.concatenate((destinations_batch.reshape(-1, 1), negatives_batch), axis=1) # (BATCH_SIZE, size+1)

        """
        interaction loop
        """
        p_pos_batch = [] # final pos item
        p_neg_batch = [] # final neg item
        edge_times_batch = []
        sources_batch_final = []

        
        for idx, (stocks_p, source, items_c, ts) in enumerate(zip(portfolios_batch, sources_batch, candidates_batch, timestamps_batch)):
          
          # ts conversion: up to the DAY
          t = str(ts)[:8] 

          if '' in stocks_p:

            # Load time features for the candidates
            cand_feature = np.array([time_feature[t][c] for c in items_c])
            cand_feature = np.log(cand_feature[:, 1:] / cand_feature[:, :-1]) # shape: (n_stocks_c, 29)

          else:
          
            # Load time features for the portfolio and candidates
            port_feature = np.array([time_feature[t][p] for p in stocks_p]) # shape: (n_stocks_p, 30)
            cand_feature = np.array([time_feature[t][c] for c in items_c])  # shape: (n_stocks_c, 30)   
            
            port_feature = np.log(port_feature[:, 1:] / port_feature[:, :-1]) # shape: (n_stocks_p, 29)
            cand_feature = np.log(cand_feature[:, 1:] / cand_feature[:, :-1]) # shape: (n_stocks_c, 29)  
        
          """
          candidates loop
          """          
          p_pos = []
          p_neg = []
          
          y_tilde_list = []

          for idx, (c, feature) in enumerate(zip(items_c, cand_feature)):

            # (1) 추천 y 계산

            y = 1 if idx==0 else 0 # pos item: 1, neg item: 0

            # (2) 투자 y_mv 계산

            # mean return of c
            mu_i = np.mean(feature) # shape: (1,)

            # covariance matrix of c and portfolio
            if '' in stocks_p:
              cov_i = np.cov(feature) # shape: (1, 1)
              sigma_i = cov_i

              # sum of sigma_ij 는 삭제한다

              # y_mv
              y_mv = (mu_i/args.gamma) / sigma_i
                
            else:
              cov_i  = np.cov(feature, port_feature) # shape: (1+n_stocks_p, 1+n_stocks_p)
              sigma_ij = cov_i[0, 1:]               # shape: (n_stocks_p,)
              sigma_i = cov_i[0, 0]                 # shape: (1,)

              # other variables
              y_uj = 1
              n_holding = len(stocks_p)

              # sum of sigma_ij
              sum_sigma_ij = y_uj/n_holding * np.sum(sigma_ij)

              # y_mv
              y_mv = (mu_i/args.gamma - 0.5*sum_sigma_ij) / sigma_i

            # (3) y_tilde 계산

            c_mv = args.gamma/2 * args.lambda_mv * sigma_i
            nominator = args.cui*y + c_mv*y_mv
            denominator = args.cui + c_mv 
            y_tilde = nominator / denominator

            # (4) y_tilde vs threshold 비교

            ### 우선은 y_mv, y_tilde 값 분포를 살펴보기 위해 값 저장해본다 ###
            if idx == 0: 
              y_mv_pos.append(y_mv)
              y_tilde_pos.append(y_tilde)
              c_mv_pos.append(c_mv)
            else:
              y_mv_neg.append(y_mv)
              y_tilde_neg.append(y_tilde)
              c_mv_neg.append(c_mv)
            
            # (5) y_tilde 저장 list
            #y_tilde_list.append(y_tilde)
            y_tilde_list.append(y_mv) # y_mv를 append 한다.
          
        
          ##### 240608 #######
          

          # Rank the elements in each list
          
          invest_rank = stats.rankdata(y_tilde_list)
          tgn_rank = stats.rankdata(np.arange(len(items_c))[::-1])
          
          # Calculate the weighted average rank for each element
          new_rank = [rank1 * args.lambda_mv + rank2 * (1-args.lambda_mv) for rank1, rank2 in zip(invest_rank, tgn_rank)]
          
          sorted_items_invest = items_c[np.argsort(y_tilde_list)[::-1]]
          sorted_items_c = items_c[np.argsort(new_rank)[::-1]]
          
          pos_to_neg_list.append([items_c[0],sorted_items_c[0]])
          invest_neg_to_neg_list.append([sorted_items_invest[-3:],sorted_items_c[-3:]]) # 마지막 3개만 저장          
          
          original_ranking_list.append(items_c)
          change_ranking_list.append(sorted_items_c)
          
          p_pos_batch.append(sorted_items_c[:args.p_pos_num])
          p_neg_batch.append(sorted_items_c[-args.p_neg_num:])
          edge_times_batch.append(ts)
          sources_batch_final.append(source)
          
      
        # flatten list of lists
        p_pos_batch = [x for y in p_pos_batch for x in y] # (BATCH_SIZE, 1) -> (BATCH_SIZE*1,)
        p_neg_batch = [x for y in p_neg_batch for x in y] # (BATCH_SIZE, k) -> (BATCH_SIZE*k,)
        

        # Convert stock codes to item idx
        p_pos_batch = [map_item_id[item] for item in p_pos_batch]   # stock code (6 digits) -> Index starting from 0
        p_pos_batch = np.array(p_pos_batch) + (upper_u + 1)         # Index starting from 0 -> item idx
        p_neg_batch = [map_item_id[item] for item in p_neg_batch]
        p_neg_batch = np.array(p_neg_batch) + (upper_u + 1)
        destinations_batch = [map_item_id[item] for item in destinations_batch]
        destinations_batch = np.array(destinations_batch) + (upper_u + 1)

        """
        emb calculation
        """
        tgn = tgn.train()
        source_embedding, p_pos_embedding, p_neg_embedding = tgn.compute_temporal_embeddings(sources_batch,
                                                                                              p_pos_batch,
                                                                                              p_neg_batch, # (BATCH_SIZE * size,)
                                                                                              timestamps_batch,
                                                                                              edge_idxs_batch,
                                                                                              args.n_degree)
        # source_embedding, destination_embedding, source_embedding_final, p_pos_embedding, p_neg_embedding = tgn.compute_temporal_embeddings_p(sources_batch,
        #                                                                                                                                       destinations_batch,
        #                                                                                                                                       sources_batch_final,
        #                                                                                                                                       p_pos_batch,
        #                                                                                                                                       p_neg_batch,
        #                                                                                                                                       timestamps_batch,
        #                                                                                                                                       edge_times_batch, # timestamps_batch,
        #                                                                                                                                       edge_idxs_batch,
        #                                                                                                                                       args.n_degree)
                                
        """
        loss calculation
        """
        bsbs = source_embedding.shape[0]

        # reshape source and destination to (bs, 1, emb_dim) 
        source_embedding = source_embedding.view(bsbs, 1, -1)
        p_pos_embedding = p_pos_embedding.view(bsbs, 1, -1)

        # reshape p_pos and p_neg to (bs, k, emb_dim) 
        # p_pos_embedding = p_pos_embedding.view(bsbs, 1, -1)
        p_neg_embedding = p_neg_embedding.view(bsbs, args.p_neg_num, -1)

        # BPR loss
        pos_scores = torch.sum(source_embedding * p_pos_embedding, dim=2)                             # (bsbs, 1)
        # pos_scores = pos_scores.unsqueeze(2).repeat(1, 1, args.p_neg_num) ##yj
        neg_scores = torch.matmul(source_embedding, p_neg_embedding.transpose(1, 2)).squeeze()        # (bsbs, k)

        score_diff = pos_scores - neg_scores                                                                # (bsbs, k)
        score_diff_mean = torch.mean(score_diff, dim=1)                                                     # (bsbs, )
        log_and_sigmoid = torch.log(torch.sigmoid(score_diff_mean))                                         # (bsbs, )
        loss_BPR = -torch.mean(log_and_sigmoid)                                                             # (1, )

        loss += loss_BPR # BACKPROP_EVERY 만큼 loss 더하기
          
      else: # baseline models
        
        # 240601 # 샘플링을 배치 내에서 하지 않고 전체에서 함
        destinations = train_data.destinations

        # negative sampling
        train_rand_sampler = RandEdgeSampler(sources_batch, destinations, portfolios_batch, upper_u, map_item_id)
        negatives_batch = train_rand_sampler.sample(size=args.p_neg_num)  # (BATCH_SIZE, size) # item idx
        """
        emb calculation
        """
        tgn = tgn.train()
        source_embedding, destination_embedding, negative_embedding = tgn.compute_temporal_embeddings(sources_batch,
                                                                                                      destinations_batch,
                                                                                                      negatives_batch.flatten(), # (BATCH_SIZE * size,)
                                                                                                      timestamps_batch,
                                                                                                      edge_idxs_batch,
                                                                                                      args.n_degree)

        """
        loss calculation
        """ 

        bsbs = source_embedding.shape[0]

        # reshape source and destination to (bs, 1, emb_dim) 
        source_embedding = source_embedding.view(bsbs, 1, -1)
        destination_embedding = destination_embedding.view(bsbs, 1, -1)

        # reshape p_pos and p_neg to (bs, k, emb_dim) 
        negative_embedding = negative_embedding.view(bsbs, args.p_neg_num, -1)

        # BPR loss

        pos_scores = torch.sum(source_embedding * destination_embedding, dim=2)                             # (bsbs, 1)
        neg_scores = torch.matmul(source_embedding, negative_embedding.transpose(1, 2)).squeeze()           # (bsbs, k)
      
        score_diff = pos_scores - neg_scores                                                                # (bsbs, k)
        score_diff_mean = torch.mean(score_diff, dim=1)                                                     # (bsbs, )
        log_and_sigmoid = torch.log(torch.sigmoid(score_diff_mean))                                         # (bsbs, )
        loss_BPR = -torch.mean(log_and_sigmoid)                                                             # (1, )

        loss += loss_BPR
        
    loss /= BACKPROP_EVERY
    if args.dyrep:
      loss.requires_grad_()
    loss.backward()
    optimizer.step()
    losses_batch.append(loss.item())
    
    

    # Detach memory after 'BACKPROP_EVERY' number of batches so we don't backpropagate to the start of time
    if USE_MEMORY:
      tgn.memory.detach_memory()
  
  # save results
  wandb.log({'loss': np.mean(losses_batch)}, step=epoch)
  time_train = time.time() - time_start; wandb.log({'time_train': time_train}, step=epoch)
  
  ### save the  ### 
  # pickle.dump(y_mv_pos, open(f'y_mv_pos.pkl', 'wb'))
  # pickle.dump(y_tilde_pos, open(f'y_tilde_pos.pkl', 'wb'))
  # pickle.dump(y_mv_neg, open(f'y_mv_neg.pkl', 'wb'))
  # pickle.dump(y_tilde_neg, open(f'y_tilde_neg.pkl', 'wb'))
  # pickle.dump(c_mv_pos, open(f'c_mv_pos.pkl', 'wb'))
  # pickle.dump(c_mv_neg, open(f'c_mv_neg.pkl', 'wb'))
  # pickle.dump(original_ranking_list, open(f'original_ranking_list.pkl', 'wb'))
  # pickle.dump(change_ranking_list, open(f'change_ranking_list.pkl', 'wb'))
  # pickle.dump(pos_to_neg_list, open(f'pos_to_neg_list_{args.lambda_mv}.pkl', 'wb'))
  # pickle.dump(invest_neg_to_neg_list, open(f'invest_neg_to_neg_list_{args.lambda_mv}.pkl', 'wb'))


  """
  Valid
  """
  # Validation uses the full graph
  tgn.set_neighbor_finder(full_ngh_finder)

  eval_dict = eval_recommendation(tgn=tgn,
                                    data=val_data, 
                                    data2 = full_data,
                                    batch_size=args.bs,
                                    n_neighbors=args.n_degree,
                                    upper_u=upper_u,
                                    NUM_NEG_EVAL = args.num_neg_eval,
                                    period=args.period,
                                    is_test_run=args.test_run,
                                    EVAL = 'valid',
                                    model_name = args.model_name,
                                    lambda_mv = args.lambda_mv
                                    )
  if USE_MEMORY:
    val_memory_backup = tgn.memory.backup_memory()

  # save results
  wandb.log(eval_dict, step=epoch)
  # results_path = f'./results/{args.prefix}/{epoch}.pkl'; pickle.dump(eval_dict, open(results_path, 'wb'))
  time_valid = time.time() - time_train; wandb.log({'time_valid': time_valid}, step=epoch)

  """
  Test
  """
  tgn.embedding_module.neighbor_finder = full_ngh_finder

  eval_dict_test = eval_recommendation(tgn=tgn,
                                        data=test_data, 
                                        data2 = full_data,
                                        batch_size=args.bs,
                                        n_neighbors=args.n_degree,
                                        upper_u=upper_u,
                                        NUM_NEG_EVAL = args.num_neg_eval,
                                        period=args.period,
                                        is_test_run=args.test_run,
                                        EVAL = 'test',
                                        model_name = args.model_name,
                                        lambda_mv = args.lambda_mv
                                        )
  # save results
  wandb.log(eval_dict_test, step=epoch)
  # results_path = f'./results/{args.prefix}/{epoch}_test.pkl'; pickle.dump(eval_dict_test, open(results_path, 'wb'))
  time_test = time.time() - time_valid; wandb.log({'time_test': time_test}, step=epoch)

wandb.finish()