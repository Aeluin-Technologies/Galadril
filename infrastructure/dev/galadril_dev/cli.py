"""One explicit operator interface replaces hand-maintained local scripts and state."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import ssl
import sys
from pathlib import Path
from threading import Event

import httpx
from pydantic import SecretStr, ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import accounts, runtime, tex_bundle
from .bootstrap import Bootstrap
from .files import BootstrapError, read
from .identity import certificates
from .telemetry import operation


class Arguments(argparse.Namespace):
    root: Path
    action: str | None
    no_input: bool
    import_env: Path | None
    openai_key_file: Path | None
    trust_ca: bool
    no_pull: bool
    email: str | None
    recreate_missing: bool
    password_file: Path | None
    subject: str | None
    tenant: str | None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "action",
        nargs="?",
        choices=(
            "init",
            "up",
            "down",
            "status",
            "doctor",
            "certificates",
            "trust",
            "bundle",
            "tenant",
            "token",
        ),
    )
    result.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[3]
    )
    result.add_argument("--no-input", action="store_true")
    result.add_argument("--import-env", type=Path)
    result.add_argument("--openai-key-file", type=Path)
    result.add_argument(
        "--trust",
        dest="trust_ca",
        action="store_true",
        help="Explicitly install the generated public CA into the OS trust store",
    )
    result.add_argument(
        "--no-pull",
        action="store_true",
        help="Explicitly reuse locally available images",
    )
    result.add_argument(
        "--email",
        help="Create/reuse an isolated test tenant and its administrator",
    )
    result.add_argument("--recreate-missing", action="store_true")
    result.add_argument(
        "--password-file",
        type=Path,
        help="Optional owner-only test password file; never passed in command arguments",
    )
    result.add_argument("--subject")
    result.add_argument("--tenant")
    return result


def private_value(path: Path) -> SecretStr:
    if path.stat().st_mode & 0o077:
        raise BootstrapError(
            "Credential input files must be owner-only (chmod 600)"
        )
    return SecretStr(read(path).decode().strip())


async def prepare_bundle(bootstrap: Bootstrap, console: Console) -> None:
    destination = bootstrap.state / "document-compiler/bundle"
    console.print(
        "Préparation du bundle Tectonic complet (ressources publiques, vérifiées et conservées hors réseau)."
    )
    cancelled = Event()
    worker = asyncio.create_task(
        asyncio.to_thread(tex_bundle.download, destination, stop=cancelled)
    )
    try:
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        try:
            await worker
        except tex_bundle.BundleError:
            pass
        raise
    bootstrap.update_environment(
        {"DOCUMENT_COMPILER_BUNDLE_SHA256": result.sha256}
    )
    console.print(f"Bundle prêt : {result.files} ressources.")


async def execute(arguments: Arguments, console: Console) -> None:
    action = arguments.action
    bootstrap = Bootstrap(arguments.root)
    key = (
        private_value(arguments.openai_key_file)
        if arguments.openai_key_file
        else None
    )
    if action is None:
        if arguments.no_input or not sys.stdin.isatty():
            raise BootstrapError(
                "Choose a command, for example: init --no-input"
            )
        console.print(
            Panel(
                "Galadril · environnement local\nUne configuration générée, des certificats vérifiés et le compilateur Tectonic isolé."
            )
        )
        action = Prompt.ask(
            "Action",
            choices=("up", "init", "doctor", "tenant", "down"),
            default="up",
            console=console,
        )
        if action in {"up", "init"} and Confirm.ask(
            "Configurer une clé OpenAI personnelle ?",
            default=False,
            console=console,
        ):
            entered = await asyncio.to_thread(
                getpass.getpass, "Clé OpenAI (masquée) : "
            )
            key = SecretStr(entered) if entered else None
        if action in {"up", "init"}:
            arguments.trust_ca = Confirm.ask(
                "Installer la CA locale dans votre magasin de confiance ?",
                default=True,
                console=console,
            )
    if action in {"init", "up", "bundle", "certificates", "trust"}:
        state = await bootstrap.initialize(
            import_env=arguments.import_env, openai_key=key
        )
        console.print(
            f"Configuration privée prête : {bootstrap.state}", markup=False
        )
        if arguments.trust_ca:
            await runtime.trust(bootstrap)
        if action == "init":
            console.print(
                "Prochaine commande : up. L'inscription locale et les clés existantes sont préservées."
            )
            if not state.openai_configured:
                console.print(
                    "Aucune clé OpenAI configurée ; ajoutez un fournisseur avant d'utiliser Pengolo."
                )
            return
    if action in {"up", "bundle"}:
        await prepare_bundle(bootstrap, console)
        if action == "bundle":
            return
    if action == "up":
        console.print(
            "Démarrage de Docker, du stockage privé et du compilateur…"
        )
        await runtime.start(bootstrap, pull=not arguments.no_pull)
        console.print(
            f"Galadril prêt : {bootstrap.environment()['AUTH_PUBLIC_ORIGIN']}",
            markup=False,
        )
        console.print("Compte de test : tenant --email demo@galadril.test")
    elif action == "down":
        await runtime.run(
            (*bootstrap.compose_arguments(), "down"),
            environment=bootstrap.environment(),
            root=bootstrap.root,
        )
        console.print(
            "Services arrêtés. Les volumes et les secrets sont conservés."
        )
    elif action in {"doctor", "status"}:
        await runtime.validate_host(bootstrap)
        items = await runtime.containers(bootstrap)
        table = Table("Service", "État", "Santé")
        for item in items:
            table.add_row(item.service, item.state, item.health or "—")
        console.print(table)
        if action == "doctor":
            await runtime.wait_for_compiler(bootstrap)
            context = ssl.create_default_context(
                cafile=str(bootstrap.state / "identity/ca.pem")
            )
            async with httpx.AsyncClient(
                verify=context,
                trust_env=False,
                timeout=10,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    bootstrap.environment()["AUTH_PUBLIC_ORIGIN"] + "/login"
                )
                if response.status_code >= 500:
                    raise BootstrapError("The HTTPS application is unavailable")
            console.print(
                "TLS vérifié ; les contrôles obligatoires du compilateur sont prêts."
            )
    elif action == "certificates":
        await asyncio.to_thread(
            certificates, bootstrap.state / "identity", renew=True
        )
        console.print(
            "Certificats renouvelés, clés préservées. Redémarrez les consommateurs TLS avec up."
        )
    elif action == "trust":
        if not arguments.trust_ca:
            await runtime.trust(bootstrap)
        console.print("CA locale installée dans le magasin de confiance.")
    elif action == "token":
        if arguments.subject is None or arguments.tenant is None:
            raise BootstrapError(
                "token requires --subject and --tenant from an existing account"
            )
        console.print(
            f"JWT de 15 minutes enregistré : {bootstrap.token(arguments.subject, arguments.tenant)}",
            markup=False,
        )
    elif action == "tenant":
        email = arguments.email
        if email is None and not arguments.no_input:
            email = Prompt.ask(
                "Adresse du compte administrateur de test", console=console
            )
        if email is None:
            raise BootstrapError("tenant requires --email")
        if arguments.no_input and arguments.password_file is None:
            raise BootstrapError(
                "Non-interactive account creation requires --password-file"
            )
        result = await accounts.enroll(
            email,
            bootstrap.environment()["AUTH_PUBLIC_ORIGIN"],
            bootstrap.state / "identity/ca.pem",
            bootstrap.state / "accounts",
            arguments.recreate_missing,
            compose_command=bootstrap.compose_arguments(),
            environment=bootstrap.environment(),
            root=bootstrap.root,
            password=private_value(arguments.password_file)
            if arguments.password_file
            else None,
        )
        token = bootstrap.token(result.owner, result.organization)
        console.print(
            f"Tenant : {result.organization}\nAdministrateur : {result.email}\nConnexion : {result.login}\nJWT de test : {token}",
            markup=False,
        )


def main() -> None:
    arguments = parser().parse_args(namespace=Arguments())
    console = Console()
    try:
        with operation(arguments.action or "wizard"):
            asyncio.run(execute(arguments, console))
    except KeyboardInterrupt:
        console.print(
            "Opération interrompue ; les données existantes sont conservées."
        )
        raise SystemExit(130) from None
    except (BootstrapError, tex_bundle.BundleError) as error:
        console.print(str(error), style="red", markup=False)
        raise SystemExit(1) from None
    except (OSError, ValueError, ValidationError, httpx.HTTPError) as error:
        console.print(
            f"Opération impossible ({type(error).__name__}). Vérifiez doctor et les services locaux.",
            style="red",
            markup=False,
        )
        raise SystemExit(1) from None
