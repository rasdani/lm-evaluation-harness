import os
import re
import sys
import yaml
import inspect
import pathlib
import functools
import subprocess
import collections
import importlib.util
import fnmatch

from typing import List, Union

import gc
import torch

from omegaconf import OmegaConf
from jinja2 import BaseLoader, Environment, StrictUndefined
from itertools import islice

from lm_eval.logger import eval_logger


def escaped_split(text, sep_char, maxsplit=-1):
    """Split text into a list on occurrences of the given separation
    character `sep_char`. The separation character may be escaped by a
    backslash to avoid splitting at that location.

    The separation character must be a string of size 1.

    If `maxsplit` is given, at most `maxsplit` splits are done (thus,
    the list will have at most `maxsplit + 1` elements). If `maxsplit`
    is not specified or less than 0, then there is no limit on the
    number of splits (all possible splits are made).
    """
    assert (
        len(sep_char) == 1
    ), "separation string must be a single character for escaped splitting"

    if maxsplit == 0:
        return text
    maxsplit = max(0, maxsplit)

    return re.split(r"(?<!\\)" + sep_char, text, maxsplit)


def simple_parse_args_string(args_string):
    """
    Parses something like
        args1=val1,arg2=val2
    Into a dictionary
    """
    args_string = args_string.strip()
    if not args_string:
        return {}
    arg_list = args_string.split(",")
    args_dict = OmegaConf.to_object(OmegaConf.from_dotlist(arg_list))
    return args_dict


def join_iters(iters):
    for iter in iters:
        yield from iter


def chunks(iter, n=0, fn=None):
    arr = []
    for i, x in enumerate(iter):
        arr.append(x)
        if len(arr) == (fn(i) if fn else n):
            yield arr
            arr = []

    if arr:
        yield arr


def group(arr, fn):
    res = collections.defaultdict(list)

    for ob in arr:
        res[fn(ob)].append(ob)

    return list(res.values())


class MultiChoice:
    def __init__(self, choices):
        self.choices = choices

    # Simple wildcard support (linux filename patterns)
    def __contains__(self, values):
        for value in values.split(","):
            if len(fnmatch.filter(self.choices, value)) == 0:
                eval_logger.warning("{} is not in task list.".format(value))
                eval_logger.info(f"Available tasks to choose:")
                for choice in self.choices:
                    eval_logger.info(f"  - {choice}")
        return True

    def __iter__(self):
        for choice in self.choices:
            yield choice


# Returns a list containing all values of the source_list that
# match at least one of the patterns
def pattern_match(patterns, source_list):
    task_names = set()
    for pattern in patterns:
        for matching in fnmatch.filter(source_list, pattern):
            task_names.add(matching)
    return sorted(list(task_names))


def general_detokenize(string):
    string = string.replace(" n't", "n't")
    string = string.replace(" )", ")")
    string = string.replace("( ", "(")
    string = string.replace('" ', '"')
    string = string.replace(' "', '"')
    string = re.sub(r" (['.,])", r"\1", string)
    return string


def get_rolling_token_windows(token_list, prefix_token, max_seq_len, context_len):
    """
    - context_len allows for a rolling window context, allowing each prediction window to potentially
      condition on some context

    :param token_list: list
        List of tokens to be PREDICTED
    :param max_seq_len: int
        max_seq_len of model (or max_seq_len we want to use)
    :param context_len: int
        Amount of desired token context for prediction. Needs to be at least 1.
    :param prefix_token: token
        Dummy token like <eos> so the first token has something to condition on
    :return: generator
        Generator of tuples
            (input_tokens, pred_tokens)
        Note: Score only the last len(pred_tokens) logits of the LM
    """
    assert 1 <= context_len <= max_seq_len
    if not token_list:
        return
    # +1 offset, going from input->preds
    pred_len = max_seq_len - context_len + 1
    predicted = 0

    # Special handling for first window: predict all tokens
    first_seq_len = min(max_seq_len, len(token_list))
    yield ([prefix_token] + token_list[: first_seq_len - 1], token_list[:first_seq_len])
    predicted += first_seq_len

    while predicted < len(token_list):
        window_pred_len = min(len(token_list) - predicted, pred_len)
        window_end = predicted + window_pred_len

        yield (
            token_list[window_end - max_seq_len - 1 : window_end - 1],
            token_list[window_end - window_pred_len : window_end],
        )
        predicted += window_pred_len


def make_disjoint_window(pair):
    """Takes output from get_rolling_token_windows and makes the context not overlap with the continuation"""
    a, b = pair
    return a[: len(a) - (len(b) - 1)], b


def select_continuation_from_batch_left_padding(
    generations: Union[List[List[int]], torch.Tensor], max_context_size: int
):
    """Select the continuation from the batch, removing prompts of different lengths.
    Args:
        generations (Union[List[List[int]], torch.Tensor]):
            A tensor or list-of-lists of shape [batch_size, sequence length].
        max_context_size (int):
            The size of the biggest context; generations will proceed from that
            index.
    Example:
        PAD     PAD Continue : The dog chased the cat  [every       day of the week]
        Riddle  me    this   : The  dog chased the  cat [yesterday] PAD PAD PAD PAD
    Output:
        [every day of the week]
        [yesterday]  PAD PAD PAD PAD
    """
    return generations[:, max_context_size:]


class Reorderer:
    def __init__(self, arr, fn):
        self.size = len(arr)
        arr = list(enumerate(arr))
        arr = group(arr, lambda x: fn(x[1]))
        # arr = [([y[0] for y in x], x[0][1]) for x in arr]
        # TODO: overhaul reorderer. It currently grouped requests by content but we don't want this
        arr = [([y[0]], x[0][1]) for x in arr for y in x]
        arr.sort(key=lambda x: fn(x[1]))

        self.arr = arr

    def get_reordered(self):
        return [x[1] for x in self.arr]

    def get_original(self, newarr):
        res = [None] * self.size
        cov = [False] * self.size

        for (inds, _), v in zip(self.arr, newarr):
            for ind in inds:
                res[ind] = v
                cov[ind] = True

        assert all(cov)

        return res


def make_table(result_dict):
    """Generate table of results."""
    from pytablewriter import MarkdownTableWriter, LatexTableWriter

    md_writer = MarkdownTableWriter()
    latex_writer = LatexTableWriter()
    md_writer.headers = ["Task", "Version", "Filter", "Metric", "Value", "", "Stderr"]
    latex_writer.headers = [
        "Task",
        "Version",
        "Filter",
        "Metric",
        "Value",
        "",
        "Stderr",
    ]

    values = []

    for k, dic in result_dict["results"].items():
        version = result_dict["versions"][k]
        for (mf), v in dic.items():
            m, _, f = mf.partition(",")
            if m.endswith("_stderr"):
                continue

            if m + "_stderr" + "," + f in dic:
                se = dic[m + "_stderr" + "," + f]
                values.append([k, version, f, m, "%.4f" % v, "±", "%.4f" % se])
            else:
                values.append([k, version, f, m, "%.4f" % v, "", ""])
            k = ""
            version = ""
    md_writer.value_matrix = values
    latex_writer.value_matrix = values

    # todo: make latex table look good
    # print(latex_writer.dumps())

    return md_writer.dumps()


def positional_deprecated(fn):
    """
    A decorator to nudge users into passing only keyword args (`kwargs`) to the
    wrapped function, `fn`.
    """

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        if len(args) != 1 if inspect.ismethod(fn) else 0:
            print(
                f"WARNING: using {fn.__name__} with positional arguments is "
                "deprecated and will be disallowed in a future version of "
                "lm-evaluation-harness!"
            )
        return fn(*args, **kwargs)

    return _wrapper


@positional_deprecated
def find_test_root(start_path: pathlib.Path) -> pathlib.Path:
    """
    Search upward in the directory tree to a maximum of three layers
    to find and return the package root (containing the 'tests' folder)
    """
    cur_path = start_path.resolve()
    max_layers = 3
    for _ in range(max_layers):
        if (cur_path / "tests" / "test_version_stable.py").exists():
            return cur_path
        else:
            cur_path = cur_path.parent.resolve()
    raise FileNotFoundError(
        f"Unable to find package root within {max_layers} upwards" + f"of {start_path}"
    )


@positional_deprecated
def run_task_tests(task_list: List[str]):
    """
    Find the package root and run the tests for the given tasks
    """
    import pytest

    package_root = find_test_root(start_path=pathlib.Path(__file__))
    task_string = " or ".join(task_list)
    args = [
        f"{package_root}/tests/test_version_stable.py",
        f"--rootdir={package_root}",
        "-k",
        f"{task_string}",
    ]
    sys.path.append(str(package_root))
    pytest_return_val = pytest.main(args)
    if pytest_return_val:
        raise ValueError(
            f"Not all tests for the specified tasks ({task_list}) ran successfully! Error code: {pytest_return_val}"
        )


def get_git_commit_hash():
    """
    Gets the git commit hash of your current repo (if it exists).
    Source: https://github.com/EleutherAI/gpt-neox/blob/b608043be541602170bfcfb8ec9bf85e8a0799e0/megatron/neox_arguments/neox_args.py#L42
    """
    try:
        git_hash = subprocess.check_output(["git", "describe", "--always"]).strip()
        git_hash = git_hash.decode()
    except subprocess.CalledProcessError:
        git_hash = None
    return git_hash


def import_function(loader, node):

    function_name = loader.construct_scalar(node)
    yaml_path = os.path.dirname(loader.name)

    module_name, function_name = function_name.split(".")
    module_path = os.path.join(yaml_path, "{}.py".format(module_name))

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    function = getattr(module, function_name)
    return function


# Add the import_function constructor to the YAML loader
yaml.add_constructor("!function", import_function)


def load_yaml_config(yaml_path):
    with open(yaml_path, "rb") as file:
        yaml_config = yaml.full_load(file)
        yaml_dir = os.path.dirname(yaml_path)

        if "include" in yaml_config:
            include_path = yaml_config["include"]
            del yaml_config["include"]

            if type(include_path) == str:
                include_path = [include_path]

            # Load from the last one first
            include_path.reverse()
            final_yaml_config = {}
            for path in include_path:

                # Assumes that path is a full path.
                # If not found, assume the included yaml
                # is in the same dir as the original yaml
                if not os.path.isfile(path):
                    path = os.path.join(yaml_dir, path)

                try:
                    included_yaml_config = load_yaml_config(path)
                    final_yaml_config.update(included_yaml_config)
                except Exception as ex:
                    # If failed to load, ignore
                    raise ex

            final_yaml_config.update(yaml_config)
            return final_yaml_config
        return yaml_config


env = Environment(loader=BaseLoader, undefined=StrictUndefined)


def apply_template(template, doc):
    rtemplate = env.from_string(template)
    return rtemplate.render(**doc)


def create_iterator(raw_iterator, rank, world_size, limit=None):
    """
    Method for creating a (potentially) sliced and limited
    iterator from a raw document iterator. Used for splitting data
    among ranks in multigpu setting or only pulling a sample of documents
    """
    return islice(raw_iterator, rank, limit, world_size)


def clear_torch_cache():
    gc.collect()
    torch.cuda.empty_cache()
