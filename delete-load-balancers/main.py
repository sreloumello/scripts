#!/usr/bin/env python3
"""
Script para deletar Load Balancers sem targets em múltiplas contas AWS.
Autenticação exclusivamente via Assume Role.
"""

import boto3
import logging
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Configuração
# ──────────────────────────────────────────────

ROLE_NAME = "luan-martins-role"

LOAD_BALANCERS = [
    {
        "account_id": "000000",
        "region": "us-east-1",
        "name": "lb-name",
    },
    {
        "account_id": "000000",
        "region": "us-east-1",
        "name": "lb-name",
    }
]

# ──────────────────────────────────────────────
# Funções
# ──────────────────────────────────────────────

def get_session(account_id: str, region: str) -> boto3.Session:
    role_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
    log.info(f"  Assumindo role: {role_arn}")
    sts = boto3.client("sts")
    resp = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="delete-lb-session",
    )
    creds = resp["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def find_lb_arn(client, lb_name: str) -> str | None:
    try:
        resp = client.describe_load_balancers(Names=[lb_name])
        lbs = resp.get("LoadBalancers", [])
        return lbs[0]["LoadBalancerArn"] if lbs else None
    except ClientError as e:
        if e.response["Error"]["Code"] == "LoadBalancerNotFound":
            log.warning(f"  Load Balancer '{lb_name}' não encontrado.")
            return None
        raise


def inspect_targets(client, lb_arn: str):
    tgs_resp = client.describe_target_groups(LoadBalancerArn=lb_arn)
    target_groups = tgs_resp.get("TargetGroups", [])

    if not target_groups:
        log.info(f"  [DRY-RUN] Nenhum target group encontrado.")
        return

    for tg in target_groups:
        tg_arn      = tg["TargetGroupArn"]
        tg_name     = tg["TargetGroupName"]
        protocol    = tg.get("Protocol", "?")
        port        = tg.get("Port", "?")

        health_resp = client.describe_target_health(TargetGroupArn=tg_arn)
        targets     = health_resp.get("TargetHealthDescriptions", [])

        log.info(f"  [DRY-RUN] Target Group: {tg_name} | {protocol}:{port}")

        if not targets:
            log.info(f"            -> Sem targets registrados (seguro deletar)")
        else:
            log.warning(f"            -> {len(targets)} target(s) registrado(s) — ATENCAO!")
            for t in targets:
                target_id = t["Target"]["Id"]
                target_port = t["Target"].get("Port", port)
                health = t["TargetHealth"]["State"]
                reason = t["TargetHealth"].get("Reason", "")
                reason_str = f" | motivo: {reason}" if reason else ""
                log.warning(f"               * {target_id}:{target_port} | status: {health}{reason_str}")


def delete_listeners(client, lb_arn: str):
    resp = client.describe_listeners(LoadBalancerArn=lb_arn)
    for listener in resp.get("Listeners", []):
        arn = listener["ListenerArn"]
        log.info(f"  Deletando listener: {arn}")
        client.delete_listener(ListenerArn=arn)


def delete_load_balancer(client, lb_arn: str, lb_name: str, dry_run: bool):
    if dry_run:
        log.info(f"  [DRY-RUN] Deletaria: {lb_name} ({lb_arn})")
        inspect_targets(client, lb_arn)
        return
    delete_listeners(client, lb_arn)
    log.info(f"  Deletando Load Balancer: {lb_name} ({lb_arn})")
    client.delete_load_balancer(LoadBalancerArn=lb_arn)
    log.info(f"  Deletado com sucesso: {lb_name}")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main(dry_run: bool = True):
    """
    dry_run=True  -> apenas lista o que seria deletado
    dry_run=False -> executa a delecao de verdade
    """
    log.info(f"=== Modo: {'DRY-RUN' if dry_run else 'EXECUCAO REAL'} ===\n")

    results = {"success": [], "not_found": [], "error": []}

    for lb in LOAD_BALANCERS:
        account_id = lb["account_id"]
        region     = lb["region"]
        name       = lb["name"]

        log.info(f"--- Conta: {account_id} | Regiao: {region} | LB: {name} ---")

        try:
            session = get_session(account_id, region)
            client  = session.client("elbv2")

            lb_arn = find_lb_arn(client, name)
            if not lb_arn:
                results["not_found"].append(name)
                continue

            delete_load_balancer(client, lb_arn, name, dry_run)
            results["success"].append(name)

        except Exception as e:
            log.error(f"Erro ao processar '{name}': {e}")
            results["error"].append({"name": name, "error": str(e)})

        print()

    log.info("=== RESUMO ===")
    log.info(f"Sucesso        ({len(results['success'])}): {results['success']}")
    log.info(f"Nao encontrado ({len(results['not_found'])}): {results['not_found']}")
    log.info(f"Erros          ({len(results['error'])}): {results['error']}")


if __name__ == "__main__":
    # mudar para dry_run=False somente quando quiser executar de verdade
    main(dry_run=True)