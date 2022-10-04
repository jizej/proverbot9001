from collections import deque
from sklearn.ensemble import RandomForestRegressor
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import wandb
import time, argparse, random
from pathlib_revised import Path2
import dataloader
import coq_serapy as serapi_instance
from coq_serapy import load_commands, kill_comments, get_hyp_type, get_indexed_vars_dict, get_stem, split_tactic
from coq_serapy.contexts import truncate_tactic_context, FullContext
from search_file import loadPredictorByFile
from search_strategies import completed_proof
from train_encoder import EncoderRNN, DecoderRNN, Lang, tensorFromSentence, EOS_token
from tokenizer import get_symbols, get_words,tokenizers
import pickle
import gym
import fasttext
import os, sys
from util import nostderr, unwrap, eprint, mybarfmt


# scraped_tactics = dataloader.scraped_tactics_from_file(str(args.scrape_file), args.max_tuples)

# #print(type(scraped_tactics), len(scraped_tactics))

# for tactics in scraped_tactics :
#     #print("Tactic", tactics.tactic.strip())
#     #print("Relavant Lemmas : ", tactics.relevant_lemmas)
#     #print("previous_tactics : ", tactics.prev_tactics)
#     #print("Proof context : ")
#     #print("    Foreground goals :" )
#     for i in tactics.context.fg_goals :
#         #print("           Hypothesis : ", i.hypotheses)
#         #print("           Goals : ", i.goal)
#     #print("    Background goals :" )
#     for i in tactics.context.bg_goals :
#         #print("           Hypothesis : ", i.hypotheses)
#         #print("           Goals : ", i.goal)
#     #print("    Shelved goals :" )
#     for i in tactics.context.shelved_goals :
#         #print("           Hypothesis : ", i.hypotheses)
#         #print("           Goals : ", i.goal)
#     #print("    Given up goals :" )
#     for i in tactics.context.given_up_goals :
#         #print("           Hypothesis : ", i.hypotheses)
#         #print("           Goals : ", i.goal)       
#     #print("The tactic : ", tactics.tactic)
#     #print()
#     #print()


class Agent_model(nn.Module) :
	def __init__(self,input_size,output_size) :
		super(Agent_model,self).__init__()
		self.lin1 = nn.Linear(input_size,1000)
		self.lin2 = nn.Linear(1000,500)
		self.lin3 = nn.Linear(500,200)
		self.lin4 = nn.Linear(200,100)
		self.lin5 = nn.Linear(100,50)
		self.lin6 = nn.Linear(100,50)
		self.lin7 = nn.Linear(100,50)
		self.lin8 = nn.Linear(100,50)
		self.linfinal = nn.Linear(50,output_size)
		self.softmax = nn.Softmax()
		self.relu = nn.LeakyReLU()
		# self.apply(self.init_weights)
		
	def forward(self,x) :
		x = self.relu(self.lin1(x))
		x = self.relu(self.lin2(x))
		x = self.relu(self.lin3(x))
		x = self.relu(self.lin4(x))
		x = self.relu(self.lin5(x))
		# x = self.relu(self.lin6(x))
		# x = self.relu(self.lin7(x))
		# x = self.relu(self.lin8(x))
		x = self.linfinal(x)
		return x

class ProofEnv(gym.Env) :
	def __init__(self, proof_file, prelude, time_per_command=100):
		self.action_space = None
		self.observation_space = None
		self.prelude= prelude
		# self.max_num_proofs = None


		self.proof_file = proof_file
		self.commands = load_commands(proof_file, progress_bar=True)
		self.proof_line_num = 0
		self.num_proofs = 0

		self.coq_running = False
		self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
		self.time_per_command= time_per_command
		self.load_state_model()
		self.load_language_model()
		self.load_list_tactic_classes()
	
	def load_list_tactic_classes(self) :
		with open("tactics.txt", "r") as f :
			whole_file = f.read()
			self.list_of_tactic_classes = whole_file.split("\n")
			for i in range(len(self.list_of_tactic_classes)) :
				self.list_of_tactic_classes[i] = self.list_of_tactic_classes[i].strip().rstrip(".")




	def goto_next_proof(self):
		# self.num_commands = 0
		while self.proof_line_num < len(self.commands) :
			#print("==-== ",self.commands[self.proof_line_num].lstrip().rstrip(), "==-=====-", self.proof_line_num)
			if self.commands[self.proof_line_num].lstrip().rstrip() == "Proof." :
				print("Found Proof : ", kill_comments(self.commands[self.proof_line_num - 1].lstrip().rstrip()))
				self.coq.run_stmt(self.commands[self.proof_line_num].lstrip().rstrip(), timeout= self.time_per_command)
				self.proof_line_num += 1
				break

			self.coq.run_stmt(self.commands[self.proof_line_num].lstrip().rstrip(), timeout= self.time_per_command)
			self.proof_line_num += 1

		
		if self.proof_line_num >= len(self.commands) : #or self.num_proofs > self.max_num_proofs:
			#print("File done")
			self.reset_to_start_of_file()
			return self.goto_next_proof()

		return self.get_state_vector_from_text( self.coq.proof_context.fg_goals[0].goal.lstrip().rstrip())

	def clear_proof_context(self) :
		while self.coq.proof_context != None :
			self.coq.cancel_last()

	def solve_curr_from_file(self) :
		self.clear_proof_context()
		while self.commands[self.proof_line_num].lstrip().rstrip() != "Proof.":
			self.proof_line_num -= 1

		self.proof_line_num -= 1
		while self.coq.proof_context != None :
			self.coq.run_stmt(self.commands[self.proof_line_num].lstrip().rstrip(), timeout= self.time_per_command)
			self.proof_line_num += 1
		
		#print(self.commands[self.proof_line_num-1])
		#print(self.commands[self.proof_line_num])
		assert self.commands[self.proof_line_num] != "Qed."
		


	def reset_to_start_of_file(self) :
		if self.coq_running :
			self.coq.kill()
		self.coq = serapi_instance.SerapiInstance(['sertop', '--implicit'],"Globalenvs", prelude = self.prelude)
		self.coq.verbose = 0
		self.coq.quiet = True
		self.proof_line_num = 0
		self.scraped_tactic_index = 0
		self.num_proofs = 0
		self.coq_running = True


	def load_state_model(self) :
		self.state_model =  torch.load("data/encoder_symbols.model", map_location=torch.device(self.device))
	def load_language_model(self) :
		with open("data/encoder_language_symbols.pkl","rb") as f:
			self.language_model = pickle.load(f)


	def get_state_vector_from_text(self,state_text) :
		state_sentence = get_symbols(state_text)
		print("State text :", state_text)
		state_tensor = tensorFromSentence(self.language_model,state_sentence,self.device, ignore_missing = True)
		with torch.no_grad() :
			state_model_hidden = self.state_model.initHidden(self.device)
			state_model_cell = self.state_model.initCell(self.device)
			input_length = state_tensor.size(0)
			for ei in range(input_length):
				_, state_model_hidden,state_model_cell = self.state_model(state_tensor[ei], state_model_hidden,state_model_cell)

			
			state= state_model_hidden
		state = state.cpu().detach().numpy().flatten()
		# state = np.append(state,[self.num_commands]).astype("float") 
		return state


	def step(self, action):
		done = True
		action_to_be_taken = self.commands[self.proof_line_num].lstrip().rstrip()


		print("This : ",action_to_be_taken,self.proof_line_num)
		if action != action_to_be_taken :
			r = -1
		else :
			r = 1

		try:
			self.coq.run_stmt(action_to_be_taken, timeout= self.time_per_command)
			self.proof_line_num += 1
		except (serapi_instance.TimeoutError, serapi_instance.ParseError,
				serapi_instance.CoqExn, serapi_instance.OverflowError,
				serapi_instance.ParseError,
				RecursionError,
				serapi_instance.UnrecognizedError) as e:
			#print("One of known errors", e)
			r = -1
			quit()
		except serapi_instance.CoqAnomaly:
			#print("Coq Anomaly")
			self.kill()
			quit()
		except :
			#print("Some error")
			self.kill()
			quit()
		else :
			
			
			if self.coq.proof_context == None :
				print("No context")
				quit()

			print(completed_proof(self.coq))
			while not (completed_proof(self.coq)) and len(self.coq.proof_context.fg_goals) == 0 :
				print("Running - ",self.commands[self.proof_line_num].lstrip().rstrip())
				self.coq.run_stmt(self.commands[self.proof_line_num].lstrip().rstrip(), timeout= self.time_per_command)
				self.proof_line_num += 1
				print(completed_proof(self.coq))
			

			if completed_proof(self.coq) :
				self.coq.run_stmt(self.commands[self.proof_line_num].lstrip().rstrip(), timeout= self.time_per_command)
				self.proof_line_num += 1
				print("Current proof fin")
				self.num_proofs += 1
				self.goto_next_proof()
				done = True
			
		next_state = self.get_state_vector_from_text( self.coq.proof_context.fg_goals[0].goal)
		return next_state, r, done, {'next_true_action' : self.commands[self.proof_line_num].lstrip().rstrip()}


	def reset(self):
		self.reset_to_start_of_file()
		state = self.goto_next_proof()
		return state, {'next_true_action' : self.commands[self.proof_line_num].lstrip().rstrip()}


class Memory :
	def __init__(self) :
		self.mem = []
		self.num_items = 0
				
	def add(self,s,a,r,sn) :
		self.mem.append([s,a,r,sn])
		self.num_items += 1
	
	def clear(self) :
		self.mem = []
		self.num_items = 0
	
	def sample_random_minibatch(self,n) :
		if n :
			mem_batch = random.sample(self.mem,n)
		else :
			mem_batch = list(self.mem)
			random.shuffle(mem_batch)
		return mem_batch


def is_hyp_token(arg, obligation) :
    
    if arg in obligation.goal and arg in obligation.hypotheses :
        print("arg in both")
        quit()
    elif arg in obligation.goal :
        return False
    elif arg in obligation.hypotheses :
        return True
    
    # print("Arg nowhere")
    return False
	
def get_epsilon_greedy(qvals,epsilon):
	coin = np.random.rand()
	if coin <= epsilon :
		return np.argmax(qvals)
	else :
		return np.random.randint(low = 0, high=len(qvals))



def get_qvals(state_actions, agent_model):
	state_actions = torch.tensor(state_actions,dtype=torch.float32)
	qvals = agent_model(state_actions)
	print(qvals)
	return qvals

def get_state_action(s, tactic_space_model, env, predictor) :
	relevant_lemmas = env.coq.local_lemmas[:-1]
	full_context_before = FullContext(relevant_lemmas, env.coq.prev_tactics,  env.coq.proof_context)
	predictions = predictor.predictKTactics(
		truncate_tactic_context(full_context_before.as_tcontext(),
								args.max_term_length), args.max_attempts)
	

	state_action = []
	for prediction_idx, prediction in enumerate(predictions):
		curr_pred = prediction.prediction.lstrip().rstrip()
		tactic_class,tactic_args = split_tactic(curr_pred.lstrip().rstrip().rstrip("."))
		# tactic_class_vec = np.eye(len(list_of_tactic_classes), 0, list_of_tactic_classes.index(tactic_class)).flatten()
		tactic_class_vec = np.zeros(len(env.list_of_tactic_classes) + 1)
		if tactic_class in env.list_of_tactic_classes :
			tactic_class_vec[ env.list_of_tactic_classes.index(tactic_class) ] = 1
		else :
			tactic_class_vec[-1] = 1

		if tactic_args.strip() != "" and is_hyp_token(tactic_args,env.coq.proof_context.fg_goals[0]) :
			# print(current_context.fg_goals[0].hypotheses)
			# print(get_indexed_vars_dict(current_context.fg_goals[0].hypotheses))
			tactic_args = tactic_args.strip()
			index = get_indexed_vars_dict(env.coq.proof_context.fg_goals[0].hypotheses)[tactic_args]
			tactic_args_type = get_hyp_type(env.coq.proof_context.fg_goals[0].hypotheses[index])
			tactic_args_type_vec = env.get_state_vector_from_text(tactic_args_type) # tactic_space_model.get_word_vector(tactic_args)
			tactic_args_vec =  tactic_space_model.get_word_vector(tactic_args)
		else :
			# print("Nope", tactic_args)
			tactic_args_type_vec = np.zeros(shape = s.shape)
			tactic_args_vec = tactic_space_model.get_word_vector(tactic_args)

		final_state_action_vec = np.concatenate((tactic_class_vec, tactic_args_type_vec, tactic_args_vec))
		state_action.append(final_state_action_vec)

	return state_action, predictions


def get_state_true_action(s, true_action, tactic_space_model, env) :
	curr_pred = true_action.strip().rstrip('.').strip()
	tactic_class,tactic_args = split_tactic(curr_pred)
	# tactic_class_vec = np.eye(len(list_of_tactic_classes), 0, list_of_tactic_classes.index(tactic_class)).flatten()
	tactic_class_vec = np.zeros(len(env.list_of_tactic_classes) + 1)
	if tactic_class in env.list_of_tactic_classes :
		tactic_class_vec[ env.list_of_tactic_classes.index(tactic_class) ] = 1
	else :
		tactic_class_vec[-1] = 1

	if tactic_args.strip() != "" and is_hyp_token(tactic_args,env.coq.proof_context.fg_goals[0]) :
		# print(current_context.fg_goals[0].hypotheses)
		# print(get_indexed_vars_dict(current_context.fg_goals[0].hypotheses))
		tactic_args = tactic_args.strip()
		index = get_indexed_vars_dict(env.coq.proof_context.fg_goals[0].hypotheses)[tactic_args]
		tactic_args_type = get_hyp_type(env.coq.proof_context.fg_goals[0].hypotheses[index])
		tactic_args_type_vec = env.get_state_vector_from_text(tactic_args_type) # tactic_space_model.get_word_vector(tactic_args)
		tactic_args_vec =  tactic_space_model.get_word_vector(tactic_args)
	else :
		# print("Nope", tactic_args)
		tactic_args_type_vec = np.zeros(shape = s.shape)
		tactic_args_vec = tactic_space_model.get_word_vector(tactic_args)

	final_state_action_vec = np.concatenate((tactic_class_vec, tactic_args_type_vec, tactic_args_vec))
	return final_state_action_vec



def select_action(s, agent_model, true_action, tactic_space_model, env, predictor, epsilon) :
	state_action, predictions = get_state_action(s, tactic_space_model, env, predictor)
	state_true_action = get_state_true_action(s,true_action,tactic_space_model, env)
	state_action.append(state_true_action)
	qvals = get_qvals(state_action, agent_model)
	action_idx = get_epsilon_greedy([i.item() for i in qvals],epsilon)

	if action_idx == len(state_action) - 1 :
		return true_action, state_action[action_idx]

	return predictions[action_idx].prediction,state_action[action_idx]


def select_random_action(s, true_action, tactic_space_model, env, predictor) :
	state_action, predictions = get_state_action(s, tactic_space_model, env, predictor)
	state_true_action = get_state_true_action(s,true_action,tactic_space_model, env)
	state_action.append(state_true_action)
	action_idx = np.random.choice(range(len(state_action)))

	if action_idx == len(state_action) - 1 :
		return true_action, state_action[action_idx]

	return predictions[action_idx].prediction,state_action[action_idx]



def Bandit(T_max, batch_size, args) :
	
	env = ProofEnv(args.proof_file.path, args.prelude)
	predictor = loadPredictorByFile(args.weightsfile)
	# tactic_space_model = fasttext.train_unsupervised(args.proof_file.path, model='cbow', lr = 0.1,epoch = 10)
	# tactic_space_model.save_model("data/action_space_model.pkl")
	tactic_space_model = fasttext.load_model("data/action_space_model.pkl")
	device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

	s,info = env.reset()



	agent_model = torch.load("data/nn_bandit_model.model").to(device)

	curr_epsilon = 1


	T = 0
	episode_r = 0
	update_every = 20
	perf_queue = deque(maxlen=update_every)

	while T <= T_max :
		# a = get_epsilon_greedy( agent_model.get_qvals(torch.tensor(s)), curr_epsilon )
		# #print(s)
		true_action = info["next_true_action"]
		prediction, state_action = select_action(s, agent_model, true_action, tactic_space_model, env, predictor, curr_epsilon)
		#print("Selected action :" +  prediction  + "; Take step <press Enter>?")
		s_next,episode_r, done, info = env.step(prediction)
		#print("Step taken")
		
		s = s_next
		T += 1
		perf_queue.append(episode_r)
		
		if args.wandb_log :                
			wandb.log({"True Rewards" : episode_r})
			wandb.log({"Exploration Factor":curr_epsilon})
			wandb.log({"T" : T})
			wandb.log({"Gain" : sum(perf_queue)/len(perf_queue)})
			wandb.log({"Num Proofs encountered" : env.num_proofs})


	

if __name__ == "__main__" :
	parser = argparse.ArgumentParser()
	parser.add_argument("--proof_file", type=Path2)
	parser.add_argument("--max-tuples", default=None, type=int)
	parser.add_argument("--tokenizer",
							choices=list(tokenizers.keys()), type=str,
							default=list(tokenizers.keys())[0])
	parser.add_argument("--num-keywords", default=100, type=int)
	parser.add_argument("--lineend", action="store_true")
	parser.add_argument('--wandb_log', action= 'store_true')
	parser.add_argument('--weightsfile', default = "data/polyarg-weights.dat", type=Path2)
	parser.add_argument("--max_term_length", type=int, default=256)
	parser.add_argument("--max_attempts", type=int, default=10)
	parser.add_argument('--prelude', default=".")


	args = parser.parse_args()

	if args.wandb_log :
		wandb.init(project="Proverbot", entity="avarghese")


	total_num_steps = 1500
	gamma = 1
	batch_size = 200
	

	Bandit(T_max= total_num_steps, args = args, batch_size = batch_size)

		