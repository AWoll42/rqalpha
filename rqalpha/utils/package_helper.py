# -*- coding: utf-8 -*-
#
# Copyright 2017 Ricequant, Inc
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .logger import system_log
from importlib import import_module


def import_mod(mod_name):
    try:
        return import_module(mod_name)
    except Exception as e:
        system_log.error("*" * 10)
        system_log.error("Mod Import Error: ")
        system_log.error(e)
        system_log.error("*" * 10)
        return None
