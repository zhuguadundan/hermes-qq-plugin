from .cli import register_cli, napcat_qq_bridge_command


def register(ctx):
    ctx.register_cli_command(
        name="napcat-qq-bridge",
        help="Run NapCat QQ bridge for Hermes",
        setup_fn=register_cli,
        handler_fn=napcat_qq_bridge_command,
        description="Receive QQ messages from NapCat and relay them to Hermes; send text/images/voice/files back to QQ.",
    )
