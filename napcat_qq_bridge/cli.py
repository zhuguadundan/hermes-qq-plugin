from .bridge import build_arg_parser, main


def register_cli(subparser):
    build_arg_parser(subparser)


def napcat_qq_bridge_command(args):
    return main(args)
