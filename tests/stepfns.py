"""Small step functions used only by the tests."""

import os


def not_json(inputs, params, ctx):
    return {"when": object()}


def read_env(inputs, params, ctx):
    return os.environ.get(params["var"])
