from typing import Any, Callable, Dict, Iterable, Optional, Tuple, Union, Hashable, Iterator
from jaxtyping import PyTree
from jax.random import KeyArray
from collections import deque
import jax
from tqdm.auto import tqdm
from JaxSeq.utils import Dataset, dataloader, create_path, match_partition_rules, get_enabled_save_path
from JaxSeq.data import Seq2SeqDataset, Seq2SeqIterableDataset
from JaxSeq.models.base_interface import Train, Inference
from JaxSeq.logs import combine_logs, label_logs, log, pull_logs
import os
import wandb
from JaxSeq.bucket_manager import open_with_bucket as open
from JaxSeq.bucket_manager import delete_with_bucket as delete
from JaxSeq.checkpointing import save_pytree
from JaxSeq.shard_model import get_sharding_from_model
from flax.training.train_state import TrainState
from transformers.modeling_flax_utils import FlaxPreTrainedModel
import pickle as pkl
from jax.sharding import NamedSharding
import jax.numpy as jnp

from torch.utils.tensorboard import SummaryWriter  # Import SummaryWriter

def log_tensorboard(writer, logs, step):
    for k, v in logs.items():
        if isinstance(v, dict):
            # modify all the keys with the header of k
            modified_dict = {f'{k}/{key}'  : value for key, value in v.items()}
            log_tensorboard(writer, modified_dict, step)
        else:
            writer.add_scalar(k, v, step)

def dump_state(
    model: FlaxPreTrainedModel, 
    train_state: TrainState, 
    save_dir: str, 
    save_train_state: bool, 
    enable_save: bool, 
    save_dtype: jnp.dtype, 
    **loop_state: Dict[Hashable, Any], 
):  
    # dump model config
    with open(get_enabled_save_path(os.path.join(save_dir, 'config.json'), enabled=enable_save), 'w') as f:
        f.write(model.config.to_json_string())
    # dump loop_state
    with open(get_enabled_save_path(os.path.join(save_dir, 'loop_state.pkl'), enabled=enable_save), 'wb') as f:
        pkl.dump(loop_state, f)
    # dump train_state
    if save_train_state:
        save_pytree(
            tree=train_state, 
            path=get_enabled_save_path(os.path.join(save_dir, 'train_state.msgpack'), enabled=enable_save), 
            dtype=save_dtype, 
            sharding=get_sharding_from_model(model, train_state), 
        )
    else:
        save_pytree(
            tree=train_state.params, 
            path=get_enabled_save_path(os.path.join(save_dir, 'params.msgpack'), enabled=enable_save), 
            dtype=save_dtype, 
            sharding=get_sharding_from_model(model, train_state.params), 
        )

def eval_loss(
    inference: Inference, 
    dataset: Union[Seq2SeqDataset, Seq2SeqIterableDataset], 
    prng_key: Optional[KeyArray], 
    bsize: int, 
    eval_batches: Optional[int], 
) -> Dict[str, Any]:
    # setup evaluator loop state
    eval_logs = []

    # eval on batches
    prng_key, new_prng = jax.random.split(prng_key) if prng_key is not None else (None, None)
    d = dataloader(new_prng, dataset, bsize, truncate=True)
    for i, batch in tqdm(enumerate(d)):
        # conditionally terminate early
        if eval_batches is not None and i >= eval_batches:
            break

        # get eval logs
        _, info = inference.eval_loss(**batch)
        eval_logs.append(info)
    
    # gather and postproc eval logs
    eval_logs = pull_logs(combine_logs(eval_logs))
    return eval_logs

def train_loop(
    trainer: Train, 
    inference: Inference, 
    evaluator: Optional[Callable[[Inference], Tuple[float, Dict[str, Any]]]], 
    dataset: Union[Seq2SeqDataset, Seq2SeqIterableDataset], 
    prng_key: KeyArray, 
    save_dir: Optional[str], 
    epochs: int, 
    max_steps: Optional[int], 
    bsize: int, 
    log_every: int, 
    eval_every_steps: Optional[int], 
    eval_every_epochs: Optional[int], 
    eval_at_beginning: bool, 
    eval_at_end: bool, 
    save_every_steps: Optional[int], 
    save_every_epochs: Optional[int], 
    save_at_beginning: bool, 
    save_at_end: bool, 
    save_best: bool, 
    max_checkpoints: Optional[int], 
    save_train_state: bool, 
    save_dtype: jnp.dtype, 
    use_wandb: bool, 
    use_tensorboard: bool,
    tensorboard_log_dir: Optional[str], 
    wandb_project: Optional[str], 
    wandb_run_name: Optional[str], 
    wandb_config: Optional[Dict[str, Any]], 
    is_main_process: Optional[bool]=None, 
    **loop_state: Dict[Hashable, Any], 
) -> Tuple[Train, Inference]:
    assert (not use_wandb) or (use_wandb and wandb_project is not None)
    if is_main_process is None:
        is_main_process = jax.process_index() == 0
    
    writer = None
    print("use_tb", use_tensorboard, "tensorboard_log_dir", tensorboard_log_dir)
    if use_tensorboard and tensorboard_log_dir:
        writer = SummaryWriter(tensorboard_log_dir)  # Create the writer
    # initalize wandb
    wandb_id = loop_state.get('wandb_id', None)
    if use_wandb and is_main_process:
        if wandb_id is None:
            wandb_id = wandb.util.generate_id()
        wandb.init(
            project=wandb_project, 
            id=wandb_id, 
            name=wandb_run_name, 
            config=wandb_config, 
            reinit=True, 
            resume="allow", 
        )

    # initalize training loop state
    train_logs = []
    best_perf = loop_state.get('best_perf', float('inf'))
    saved_checkpoints = loop_state.get('saved_checkpoints', deque([]))
    step = 0
    steps_per_epoch = len(dataset) // bsize if isinstance(dataset, Dataset) else None
    if 'steps_per_epoch' in loop_state:
        assert steps_per_epoch == loop_state['steps_per_epoch'], 'loop_state steps_per_epoch does not match dataset steps_per_epoch'
    epoch = -1

    def _save(
        name: str, 
        add_to_queue: bool, 
        **loop_state: Dict[Hashable, Any], 
    ):
        nonlocal saved_checkpoints
        print(f'saving checkpoint {name} ...')
        # conditionally delete old checkpoints
        if add_to_queue and is_main_process:
            if (max_checkpoints is not None) and (len(saved_checkpoints) >= max_checkpoints):
                delete(saved_checkpoints.popleft(), recursive=True)
        curr_save_dir = os.path.join(save_dir, name)
        if is_main_process:
            create_path(curr_save_dir)
        dump_state(
            model=trainer.model, 
            train_state=trainer.train_state, 
            save_dir=curr_save_dir, 
            save_train_state=save_train_state, 
            enable_save=is_main_process, 
            save_dtype=save_dtype, 
            **loop_state, 
        )
        if add_to_queue and is_main_process:
            saved_checkpoints.append(curr_save_dir)
        print('saved.')
    
    def _eval(
        **loop_state: Dict[Hashable, Any], 
    ):
        nonlocal best_perf
        nonlocal inference
        # get eval logs
        inference = inference.replace(params=trainer.train_state.params)
        eval_perf, eval_logs = evaluator(inference)

        # publish eval logs
        eval_logs = pull_logs(label_logs(eval_logs, 'eval', {'step': step+1, 'epoch': epoch}))
        log(eval_logs, use_wandb and is_main_process)
        if use_tensorboard:
            log_tensorboard(writer, eval_logs, step+1)


        # conditionally save best model and optimizer state
        if save_dir is not None and save_best and eval_perf < best_perf:
            print('new best model!')
            best_perf = eval_perf
            _save(
                name='best', 
                add_to_queue=False, 
                **{**loop_state, 'best_perf': best_perf}, 
            )
    
    # begin evaluation
    if evaluator is not None and eval_at_beginning:
        _eval(
            # loop state metadata
            best_perf=best_perf, 
            step=step, 
            epoch=epoch,  
            saved_checkpoints=saved_checkpoints, 
            steps_per_epoch=steps_per_epoch, 
            wandb_id=wandb_id, 
        )
    
    # save initial checkpoint
    if save_dir is not None and save_at_beginning:
        _save(
            name='initial', 
            add_to_queue=False, 
            # loop state metadata
            best_perf=best_perf, 
            step=step, 
            epoch=epoch, 
            saved_checkpoints=saved_checkpoints, 
            steps_per_epoch=steps_per_epoch, 
            wandb_id=wandb_id, 
        )
    
    # begin training loop
    for epoch in tqdm(range(epochs)):
        prng_key, new_prng = jax.random.split(prng_key)
        d = dataloader(new_prng, dataset, bsize, truncate=True)
        for batch in tqdm(d, total=steps_per_epoch):
            
            # step model and get training logs
            prng_key, new_prng = jax.random.split(prng_key)
            if 'step' in loop_state and step < loop_state['step']:
                step += 1
                continue
            trainer, _, info = trainer.step(
                **batch, 
                prng_key=new_prng, 
                train=True, 
            )
            train_logs.append(info)
            
            # publish training logs and clear logs
            if (step + 1) % log_every == 0:
                logs = combine_logs(train_logs)
                logs = pull_logs(label_logs(logs, 'train', {'step': step+1, 'epoch': epoch}))
                print(logs)
                log(logs, use_wandb and is_main_process)
                print("Attempting to log to tensorboard")
                if use_tensorboard:
                    print("logging to tensorboard", logs)
                    log_tensorboard(writer, logs, step+1)
                train_logs = []
            
            # begin evaluation
            if evaluator is not None and eval_every_steps is not None and (step + 1) % eval_every_steps == 0:
                _eval(
                    # loop state metadata
                    best_perf=best_perf, 
                    step=step+1, 
                    epoch=epoch, 
                    saved_checkpoints=saved_checkpoints, 
                    steps_per_epoch=steps_per_epoch, 
                    wandb_id=wandb_id, 
                )
            
            # periodically save checkpoint
            if save_dir is not None and save_every_steps is not None and (step + 1) % save_every_steps == 0:
                _save(
                    name=f'step_{step+1}', 
                    add_to_queue=True, 
                    # loop state metadata
                    best_perf=best_perf, 
                    step=step+1, 
                    epoch=epoch, 
                    saved_checkpoints=saved_checkpoints, 
                    steps_per_epoch=steps_per_epoch, 
                    wandb_id=wandb_id, 
                )

            step += 1

            # conditionally terminate
            if max_steps is not None and step >= max_steps:
                break
        
        # begin evaluation
        if evaluator is not None and eval_every_epochs is not None and (epoch + 1) % eval_every_epochs == 0:
            _eval(
                # loop state metadata
                best_perf=best_perf, 
                step=step, 
                epoch=epoch, 
                saved_checkpoints=saved_checkpoints, 
                steps_per_epoch=steps_per_epoch, 
                wandb_id=wandb_id, 
            )
        
        # periodically save checkpoint
        if save_dir is not None and save_every_epochs is not None and (epoch + 1) % save_every_epochs == 0:
            _save(
                name=f'epoch_{epoch}', 
                add_to_queue=True, 
                # loop state metadata
                best_perf=best_perf, 
                step=step, 
                epoch=epoch, 
                saved_checkpoints=saved_checkpoints, 
                steps_per_epoch=steps_per_epoch, 
                wandb_id=wandb_id, 
            )
        
        # conditionally terminate
        if max_steps is not None and step >= max_steps:
            break
    
    # begin evaluation
    if evaluator is not None and eval_at_end:
        _eval(
            # loop state metadata
            best_perf=best_perf, 
            step=step, 
            epoch=epoch, 
            saved_checkpoints=saved_checkpoints, 
            steps_per_epoch=steps_per_epoch, 
            wandb_id=wandb_id, 
        )
    
    # save final checkpoint
    if save_dir is not None and save_at_end:
        _save(
            name='last', 
            add_to_queue=False, 
            # loop state metadata
            best_perf=best_perf, 
            step=step, 
            epoch=epoch, 
            saved_checkpoints=saved_checkpoints, 
            steps_per_epoch=steps_per_epoch, 
            wandb_id=wandb_id, 
        )

    # stop wandb
    if use_wandb and is_main_process:
        wandb.finish()
    
    # stop tensorboard
    if writer:
        writer.close()
    
    inference = inference.replace(params=trainer.train_state.params)
    return trainer, inference
