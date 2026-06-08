#!/usr/bin/env python3

import boto3
import time
import json
import logging
from datetime import datetime, timezone
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Configuração de logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuração da conta jump e role
# ---------------------------------------------------------------------------
JUMP_PROFILE  = "jump-acct.jump.aws"                   # profile AWS CLI da conta jump (~/.aws/credentials)
ROLE_NAME     = "luan-martins-darede"  # role a assumir nas contas alvo — ajuste se necessário
ROLE_SESSION  = "ami-delete-session"     # nome da sessão STS (aparece no CloudTrail)

# ---------------------------------------------------------------------------
# Instâncias a processar
# ---------------------------------------------------------------------------
INSTANCES = [
    {
        "account_id":  "xxxxxxxxxxxxxxx",
        "region":      "us-east-1",
        "instance_id": "i-xxxxxxxxxxxxxxx",
        "name":        "xxxxxxxxxxxxxxx-xxxxxxxxxxxxxxx",
    }
]

# ---------------------------------------------------------------------------
# Parâmetros gerais
# ---------------------------------------------------------------------------
AMI_WAIT_INTERVAL_SEC = 30      # intervalo entre polls do status da AMI
AMI_WAIT_TIMEOUT_SEC  = 3600    # timeout máximo aguardando AMI (1 hora)
DRY_RUN               = False   # True = apenas simula, sem ações destrutivas

# ---------------------------------------------------------------------------
# Autenticação via AssumeRole
# ---------------------------------------------------------------------------

def assume_role(account_id: str, region: str):
    """
    Parte da conta jump (JUMP_PROFILE) e assume ROLE_NAME na conta alvo.
    Retorna um cliente EC2 autenticado na conta/região de destino.
    """
    role_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
    log.info(f"  AssumeRole → {role_arn}")

    jump_session = boto3.Session(profile_name=JUMP_PROFILE)
    sts = jump_session.client("sts")

    try:
        creds = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=ROLE_SESSION,
            DurationSeconds=3600,
        )["Credentials"]
    except ClientError as e:
        raise RuntimeError(
            f"Falha ao assumir role {role_arn}: {e.response['Error']['Message']}"
        ) from e

    target_session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
    return target_session.client("ec2")


# ---------------------------------------------------------------------------
# Helpers de EC2
# ---------------------------------------------------------------------------

def get_instance_volumes(ec2, instance_id: str) -> list[dict]:
    """Retorna lista de volumes EBS attachados à instância."""
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = resp.get("Reservations", [])
    if not reservations:
        raise ValueError(f"Instância {instance_id} não encontrada.")
    instance = reservations[0]["Instances"][0]
    volumes = []
    for bdm in instance.get("BlockDeviceMappings", []):
        ebs = bdm.get("Ebs", {})
        vol_id = ebs.get("VolumeId")
        if vol_id:
            volumes.append({
                "volume_id":            vol_id,
                "device":               bdm["DeviceName"],
                "delete_on_termination": ebs.get("DeleteOnTermination", False),
            })
    return volumes


def create_ami(ec2, instance_id: str, name: str) -> str:
    """Cria AMI sem reboot e retorna o AMI ID."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    ami_name  = f"backup-{name}-{timestamp}"
    description = (
        f"backup de {instance_id}."
    )
    log.info(f"  Criando AMI '{ami_name}' para {instance_id}...")

    if DRY_RUN:
        log.warning("  [DRY RUN] CreateImage seria executado aqui.")
        return "ami-DRYRUN00000000000"

    resp = ec2.create_image(
        InstanceId=instance_id,
        Name=ami_name,
        Description=description,
        NoReboot=True,
        TagSpecifications=[
            {
                "ResourceType": "image",
                "Tags": [
                    {"Key": "Name",           "Value": ami_name},
                    {"Key": "SourceInstance", "Value": instance_id},
                    {"Key": "CreatedBy",      "Value": "create_ami_and_delete_ec2.py"},
                    {"Key": "Chamado",        "Value": "Angelina-exclusao-instancias"},
                ],
            },
            {
                "ResourceType": "snapshot",
                "Tags": [
                    {"Key": "Name",           "Value": f"snap-{ami_name}"},
                    {"Key": "SourceInstance", "Value": instance_id},
                    {"Key": "CreatedBy",      "Value": "create_ami_and_delete_ec2.py"},
                ],
            },
        ],
    )
    ami_id = resp["ImageId"]
    log.info(f"  AMI iniciada: {ami_id}")
    return ami_id


def wait_ami_available(ec2, ami_id: str) -> bool:
    """Aguarda AMI atingir estado 'available'. Retorna True se OK, False se timeout/erro."""
    if DRY_RUN:
        log.warning("  [DRY RUN] Pulando espera de AMI.")
        return True

    log.info(f"  Aguardando AMI {ami_id} ficar disponível...")
    elapsed = 0
    while elapsed < AMI_WAIT_TIMEOUT_SEC:
        resp   = ec2.describe_images(ImageIds=[ami_id])
        images = resp.get("Images", [])
        if not images:
            log.warning(f"  AMI {ami_id} ainda não aparece no describe_images...")
        else:
            state = images[0]["State"]
            log.info(f"  AMI {ami_id} → estado: {state} ({elapsed}s)")
            if state == "available":
                return True
            if state in ("failed", "error", "deregistered"):
                log.error(f"  AMI {ami_id} entrou em estado inválido: {state}")
                return False
        time.sleep(AMI_WAIT_INTERVAL_SEC)
        elapsed += AMI_WAIT_INTERVAL_SEC

    log.error(f"  Timeout aguardando AMI {ami_id} após {AMI_WAIT_TIMEOUT_SEC}s.")
    return False


def terminate_instance(ec2, instance_id: str):
    """Termina a instância EC2."""
    log.info(f"  Terminando instância {instance_id}...")
    if DRY_RUN:
        log.warning("  [DRY RUN] TerminateInstances seria executado aqui.")
        return
    ec2.terminate_instances(InstanceIds=[instance_id])
    log.info(f"  Instância {instance_id} enviada para terminação.")


def wait_instance_terminated(ec2, instance_id: str, timeout: int = 600):
    """Aguarda a instância atingir estado 'terminated'."""
    if DRY_RUN:
        return
    log.info(f"  Aguardando instância {instance_id} ser terminada...")
    elapsed  = 0
    interval = 15
    while elapsed < timeout:
        resp  = ec2.describe_instances(InstanceIds=[instance_id])
        state = resp["Reservations"][0]["Instances"][0]["State"]["Name"]
        log.info(f"  Instância {instance_id}: {state} ({elapsed}s)")
        if state == "terminated":
            return
        time.sleep(interval)
        elapsed += interval
    log.warning(f"  Timeout aguardando terminação de {instance_id}.")


def delete_orphan_volumes(ec2, volumes: list[dict]):
    """
    Deleta volumes com DeleteOnTermination=False (órfãos após terminação).
    Volumes com DeleteOnTermination=True são removidos automaticamente pela AWS.
    """
    for vol in volumes:
        vol_id = vol["volume_id"]
        dot    = vol["delete_on_termination"]

        if dot:
            log.info(f"  Volume {vol_id} ({vol['device']}): DeleteOnTermination=True — AWS remove automaticamente.")
            continue

        log.info(f"  Volume {vol_id} ({vol['device']}): DeleteOnTermination=False — deletando manualmente...")
        if DRY_RUN:
            log.warning(f"  [DRY RUN] DeleteVolume {vol_id} seria executado aqui.")
            continue

        # Aguarda ficar 'available' após detach automático pela terminação
        for _ in range(40):
            try:
                desc  = ec2.describe_volumes(VolumeIds=[vol_id])
                state = desc["Volumes"][0]["State"]
                if state == "available":
                    break
                log.info(f"  Volume {vol_id}: {state} — aguardando 'available'...")
            except ClientError as e:
                if e.response["Error"]["Code"] == "InvalidVolume.NotFound":
                    log.info(f"  Volume {vol_id} já não existe.")
                    break
                raise
            time.sleep(15)

        try:
            ec2.delete_volume(VolumeId=vol_id)
            log.info(f"  Volume {vol_id} deletado com sucesso.")
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "InvalidVolume.NotFound":
                log.info(f"  Volume {vol_id} não encontrado — já deletado.")
            else:
                log.error(f"  Erro ao deletar volume {vol_id}: {e}")


# ---------------------------------------------------------------------------
# Processamento por instância
# ---------------------------------------------------------------------------

def process_instance(entry: dict) -> dict:
    account  = entry["account_id"]
    region   = entry["region"]
    inst_id  = entry["instance_id"]
    name     = entry["name"]

    result = {
        "account_id":  account,
        "region":      region,
        "instance_id": inst_id,
        "name":        name,
        "ami_id":      None,
        "ami_ok":      False,
        "terminated":  False,
        "volumes":     [],
        "error":       None,
    }

    log.info("=" * 70)
    log.info(f"Processando: {name} ({inst_id}) | Conta: {account} | Região: {region}")
    log.info("=" * 70)

    try:
        # 1. Autenticar via AssumeRole a partir da conta jump
        ec2 = assume_role(account, region)

        # 2. Listar volumes antes de terminar
        volumes = get_instance_volumes(ec2, inst_id)
        result["volumes"] = volumes
        log.info(f"  Volumes: {[v['volume_id'] for v in volumes]}")

        # 3. Criar AMI
        ami_id = create_ami(ec2, inst_id, name)
        result["ami_id"] = ami_id

        # 4. Aguardar AMI disponível
        ami_ok = wait_ami_available(ec2, ami_id)
        result["ami_ok"] = ami_ok

        if not ami_ok:
            log.error(f"  AMI não ficou disponível — ABORTANDO exclusão de {inst_id} por segurança.")
            result["error"] = "AMI não disponível — instância NÃO foi deletada."
            return result

        # 5. Terminar instância
        terminate_instance(ec2, inst_id)
        wait_instance_terminated(ec2, inst_id)
        result["terminated"] = True

        # 6. Deletar volumes órfãos
        delete_orphan_volumes(ec2, volumes)

        log.info(f"  ✅ {inst_id} concluída. AMI: {ami_id}")

    except Exception as e:
        log.error(f"  ❌ Erro ao processar {inst_id}: {e}", exc_info=True)
        result["error"] = str(e)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("╔══════════════════════════════════════════════════════════════════╗")
    log.info("║       create_ami_and_delete_ec2.py — DaRede / OpenFinance        ║")
    log.info("╚══════════════════════════════════════════════════════════════════╝")
    log.info(f"Conta jump: profile='{JUMP_PROFILE}' | Role alvo: {ROLE_NAME}")
    if DRY_RUN:
        log.warning("*** MODO DRY RUN ATIVO — nenhuma ação destrutiva será executada ***")

    results = []
    for entry in INSTANCES:
        result = process_instance(entry)
        results.append(result)

    # Resumo final
    log.info("\n" + "=" * 70)
    log.info("RESUMO FINAL")
    log.info("=" * 70)
    for r in results:
        status = "✅ OK" if (r["ami_ok"] and r["terminated"]) else "❌ FALHA"
        log.info(
            f"{status} | {r['name']} ({r['instance_id']}) | "
            f"AMI: {r['ami_id']} | Terminada: {r['terminated']}"
        )
        if r["error"]:
            log.error(f"       Erro: {r['error']}")

    # Salvar resultado em JSON para evidência no chamado
    output_file = f"resultado_exclusao_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"\nResultado salvo em: {output_file}")


if __name__ == "__main__":
    main()