"""rpacore entry point — the developer's wiring layer.

This file is the first thing to edit when adapting rpacore to a real automation.
Replace the example skills and transaction below with your own.
"""

from __future__ import annotations

from rpacore import (
    Engine,
    ProcessContext,
    Transaction,
    build_credential_provider,
    build_notifiers,
    configure_logger,
    dispatch,
    generate_report,
    load_config,
    save_transaction,
)

# --- Replace these with your own Skill subclasses ---
from examples.sample_skill import ConfirmOutput, ValidateInput, WriteGreeting


def main(
    *,
    config_path: str = "config.toml",
    transaction_db_path: str | None = None,
    output_path: str | None = None,
) -> None:
    # 1. Load configuration from config.toml (falls back to defaults if absent).
    #    Pass config_path to point at a different file (useful for testing).
    config = load_config(config_path)

    # 2. Configure the logger once for the entire run.
    configure_logger(level=str(config["log_level"]), fmt=str(config["log_format"]))

    # 3. Build the transaction.
    #    Explicitly instantiate skills so the wiring is readable and greppable.
    #    Replace these with your own skills.
    _output_path = output_path if output_path is not None else "greeting.txt"
    arguments = {"name": "Alice", "output_path": _output_path}

    transaction = Transaction(reference="greet-alice")
    transaction.skills = [
        ValidateInput(name="validate_input", execution_order=1, arguments=arguments),
        WriteGreeting(name="write_greeting", execution_order=2, arguments=arguments),
        ConfirmOutput(name="confirm_output", execution_order=3, arguments=arguments),
    ]

    # 4. Run the engine.
    engine = Engine(
        max_retries=int(config["max_retries"]),
        retry_delay=float(config["retry_delay"]),
        retry_backoff=float(config["retry_backoff"]),
        screenshot_dir=str(config["screenshot_dir"]),
    )
    credentials = build_credential_provider(str(config["credential_provider"]))
    ctx = ProcessContext(transaction=transaction, config=config, credentials=credentials)
    engine.run(ctx)

    # 5. Persist the result so failed runs can be inspected (and resumed).
    #    transaction_db_path defaults to config value; pass an override for testing.
    _db_path = (
        transaction_db_path
        if transaction_db_path is not None
        else str(config["transaction_db_path"])
    )
    save_transaction(transaction, db_path=_db_path)

    # 6. Dispatch notifications (email / webhook) if configured.
    notifiers = build_notifiers(config, credentials)
    if notifiers:
        report = generate_report(transaction)
        dispatch(notifiers, report)

    print(f"Transaction {transaction.id}: {transaction.status}")


if __name__ == "__main__":
    main()
