#!/usr/bin/env python3
"""
Script para deletar Elastic IPs não utilizados na AWS via Switch Role (Jump Account).

Uso:
    python main.py [--dry-run] [--jump-profile PROFILE] [--role-name ROLE_NAME]

Dependências:
    pip install boto3

Configuração prévia:
    Configure suas credenciais da conta jump no ~/.aws/credentials ou via env vars:
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN
"""

import boto3
import argparse
import logging
from botocore.exceptions import ClientError

# ─────────────────────────────────────────────
# Configuração de logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Lista de EIPs para deletar
# ─────────────────────────────────────────────
EIPS_TO_DELETE = [
    # (account_id, region, allocation_id, nome, public_ip)
]


# ─────────────────────────────────────────────
# Funções auxiliares
# ─────────────────────────────────────────────

def assume_role(jump_session: boto3.Session, account_id: str, role_name: str) -> boto3.Session:
    """
    Faz assume role na conta alvo a partir da sessão da conta jump.
    Retorna uma nova sessão com as credenciais temporárias.
    """
    sts = jump_session.client("sts")
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    log.info(f"  Assumindo role: {role_arn}")
    response = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="delete-unused-eips",
        DurationSeconds=900,  # 15 minutos
    )

    creds = response["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def release_eip(session: boto3.Session, region: str, allocation_id: str,
                public_ip: str, nome: str, dry_run: bool) -> bool:
    """
    Verifica se o EIP ainda está desassociado e o libera.
    Retorna True em caso de sucesso (ou dry-run), False em caso de erro.
    """
    ec2 = session.client("ec2", region_name=region)

    # Confirma que o EIP existe e não está associado antes de deletar
    try:
        resp = ec2.describe_addresses(AllocationIds=[allocation_id])
        addresses = resp.get("Addresses", [])

        if not addresses:
            log.warning(f"    EIP {allocation_id} ({public_ip}) não encontrado — pode já ter sido removido.")
            return False

        eip = addresses[0]
        association_id = eip.get("AssociationId")
        instance_id    = eip.get("InstanceId")
        network_iface  = eip.get("NetworkInterfaceId")

        if association_id or instance_id or network_iface:
            log.warning(
                f"    PULANDO {allocation_id} ({public_ip}) — ainda associado! "
                f"AssociationId={association_id}, InstanceId={instance_id}, "
                f"NetworkInterfaceId={network_iface}"
            )
            return False

    except ClientError as e:
        log.error(f"    Erro ao descrever EIP {allocation_id}: {e}")
        return False

    if dry_run:
        log.info(f"    [DRY-RUN] Liberaria EIP {allocation_id} ({public_ip}) | Nome: {nome}")
        return True

    # Efetua a liberação
    try:
        ec2.release_address(AllocationId=allocation_id)
        log.info(f"    ✅ EIP liberado: {allocation_id} ({public_ip}) | Nome: {nome}")
        return True
    except ClientError as e:
        log.error(f"    ❌ Falha ao liberar {allocation_id} ({public_ip}): {e}")
        return False


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Deleta Elastic IPs não utilizados via Switch Role a partir de uma conta jump."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula as ações sem efetuar nenhuma deleção real.",
    )
    parser.add_argument(
        "--jump-profile",
        default="default",
        help="Nome do perfil AWS (~/.aws/credentials) da conta jump. Padrão: default",
    )
    parser.add_argument(
        "--role-name",
        default="luan-martins-darede",
        help=(
            "Nome da role a ser assumida em cada conta alvo. "
            "Padrão: luan-martins-darede"
        ),
    )
    args = parser.parse_args()

    mode = "[DRY-RUN] " if args.dry_run else ""
    log.info(f"{mode}Iniciando limpeza de Elastic IPs não utilizados")
    log.info(f"  Jump profile : {args.jump_profile}")
    log.info(f"  Role name    : {args.role_name}")
    log.info(f"  Total de EIPs: {len(EIPS_TO_DELETE)}")

    # Sessão base da conta jump
    jump_session = boto3.Session(profile_name=args.jump_profile)

    # Agrupa EIPs por conta para minimizar chamadas de assume_role
    from collections import defaultdict
    by_account: dict[str, list] = defaultdict(list)
    for account_id, region, alloc_id, nome, public_ip in EIPS_TO_DELETE:
        by_account[account_id].append((region, alloc_id, nome, public_ip))

    success_count = 0
    failure_count = 0

    for account_id, eips in by_account.items():
        log.info(f"\n{'='*60}")
        log.info(f"Conta: {account_id} ({len(eips)} EIP(s))")
        log.info(f"{'='*60}")

        try:
            target_session = assume_role(jump_session, account_id, args.role_name)
        except ClientError as e:
            log.error(f"  Não foi possível assumir role na conta {account_id}: {e}")
            failure_count += len(eips)
            continue

        for region, alloc_id, nome, public_ip in eips:
            log.info(f"  Região: {region} | {alloc_id} ({public_ip}) | Nome: {nome}")
            ok = release_eip(target_session, region, alloc_id, public_ip, nome, args.dry_run)
            if ok:
                success_count += 1
            else:
                failure_count += 1

    # Resumo
    total_cost_saved = success_count * 3.65
    log.info(f"\n{'='*60}")
    log.info(f"{'[DRY-RUN] ' if args.dry_run else ''}Resumo")
    log.info(f"  ✅ Sucesso  : {success_count}")
    log.info(f"  ❌ Falhas   : {failure_count}")
    log.info(f"  💰 Economia estimada: ~${total_cost_saved:.2f}/mês")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    main()