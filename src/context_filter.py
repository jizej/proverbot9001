#!/usr/bin/env python3

from typing import Dict, Callable, Union, List
import re

ContextData = Dict[str, Union[str, List[str]]]
ContextFilter = Callable[[ContextData, str, ContextData], bool]

def filter_and(f1 : ContextFilter, f2 : ContextFilter) -> ContextFilter:
    return lambda in_data, tactic, next_in_data: (f1(in_data, tactic, next_in_data) and
                                                  f2(in_data, tactic, next_in_data))
def filter_or(f1 : ContextFilter, f2 : ContextFilter) -> ContextFilter:
    return lambda in_data, tactic, next_in_data: (f1(in_data, tactic, next_in_data) or
                                                  f2(in_data, tactic, next_in_data))

def no_compound_or_bullets(in_data : ContextData, tactic : str,
                           next_in_data : ContextData) -> bool:
    return (not re.match("\s*[\{\}\+\-\*].*", tactic, flags=re.DOTALL) and
            not re.match(".*;.*", tactic, flags=re.DOTALL))

def goal_changed(in_data : ContextData, tactic : str,
                 next_in_data : ContextData) -> bool:
    return in_data["goal"] != next_in_data["goal"]

def hyps_changed(in_data : ContextData, tactic : str,
                 next_in_data : ContextData) -> bool:
    return in_data["hyps"] != next_in_data["hyps"]

def no_args(in_data : ContextData, tactic : str,
            next_in_data : ContextData) -> bool:
    return re.match("\s*\S*\.", tactic) != None

context_filters : Dict[str, ContextFilter] = {
    "default": no_compound_or_bullets,
    "all": lambda *args: True,
    "goal-changes": filter_and(goal_changed, no_compound_or_bullets),
    "hyps-change": filter_and(hyps_changed, no_compound_or_bullets),
    "something-changes":filter_and(filter_or(goal_changed, hyps_changed),
                                   no_compound_or_bullets),
    "no-args": filter_and(no_args, no_compound_or_bullets),
}
