#!/usr/bin/env python3
"""
delete_nat_gateways.py
======================
Deleta NAT Gateways via SSO cross-account com dry-run inteligente.

Dry-run verifica route tables, subnets e workloads que ainda usam o NAT
antes de qualquer alteração.

Uso:
    python main.py --dry-run     # apenas analisa
    python main.py --execute     # deleta após confirmação

Pré-requisitos:
    pip install boto3
    aws sso login --profile jump-acct.jump.aws
"""

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import boto3
from botocore.exceptions import ClientError

# ─── Cores ANSI ──────────────────────────────────────────────────────────────

RED    = "\033[0;31m"
YELLOW = "\033[1;33m"
GREEN  = "\033[0;32m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def log(msg):  print(f"{CYAN}[INFO]{RESET}  {msg}")
def warn(msg): print(f"{YELLOW}[WARN]{RESET}  {msg}")
def err(msg):  print(f"{RED}[ERROR]{RESET} {msg}")
def ok(msg):   print(f"{GREEN}[OK]{RESET}    {msg}")
def sep():     print(f"{BOLD}{'─' * 72}{RESET}")


# ─── Configuração SSO ────────────────────────────────────────────────────────

SSO_PROFILE = "jump-acct.jump.aws"
ROLE_NAME   = "luan-martins-darede"   # ajuste se necessário


# ─── Lista de NATs a processar ───────────────────────────────────────────────

@dataclass
class NatTarget:
    account_id: str
    region: str
    nat_id: str
    name: str

NAT_TARGETS = [
    # Exemplo:
    # NatTarget(account_id="123456789012", region="us-east-1", nat_id="nat-0abc123def456ghi7", name="NAT Gateway 1"),
]


# ─── Assume Role ─────────────────────────────────────────────────────────────

def assume_role(account_id: str, region: str) -> Optional[boto3.Session]:
    """
    Assume role na conta-alvo via SSO jump account.
    Tenta o nome exato primeiro; se falhar, descobre o hash suffix via IAM.
    """
    jump_session = boto3.Session(profile_name=SSO_PROFILE)
    sts = jump_session.client("sts", region_name=region)

    def _try_assume(role_arn: str) -> Optional[dict]:
        try:
            return sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=f"nat-cleanup-{int(time.time())}",
            )["Credentials"]
        except ClientError:
            return None

    # 1ª tentativa — nome sem hash
    role_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
    creds = _try_assume(role_arn)

    # 2ª tentativa — descobre hash suffix via IAM na jump account
    if not creds:
        try:
            iam = jump_session.client("iam")
            paginator = iam.get_paginator("list_roles")
            for page in paginator.paginate(PathPrefix="/aws-reserved/sso.amazonaws.com/"):
                for role in page["Roles"]:
                    rname = role["RoleName"]
                    if "AdministratorAccess" in rname:
                        candidate = f"arn:aws:iam::{account_id}:role/{rname}"
                        creds = _try_assume(candidate)
                        if creds:
                            break
                if creds:
                    break
        except ClientError:
            pass

    if not creds:
        return None

    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


# ─── Verificações de uso ──────────────────────────────────────────────────────

@dataclass
class UsageReport:
    route_tables: list = field(default_factory=list)   # [{id, name, subnets[]}]
    ec2_instances: list = field(default_factory=list)  # [{id, state, type, name, subnet}]
    lambdas: list = field(default_factory=list)        # [{name, state}]
    ecs_tasks: list = field(default_factory=list)      # [{task_id, cluster, status}]
    rds_instances: list = field(default_factory=list)  # [{id, engine, status, cls}]
    eks_clusters: list = field(default_factory=list)   # [{name, vpc}]

    @property
    def in_use(self) -> bool:
        return bool(self.route_tables)


def _tag_name(tags: list) -> str:
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", "")
    return "sem-nome"


def check_route_tables(ec2, nat_id: str) -> list:
    rts = []
    resp = ec2.describe_route_tables(
        Filters=[{"Name": "route.nat-gateway-id", "Values": [nat_id]}]
    )
    for rt in resp.get("RouteTables", []):
        subnets = [
            a["SubnetId"]
            for a in rt.get("Associations", [])
            if "SubnetId" in a
        ]
        rts.append({
            "id":      rt["RouteTableId"],
            "name":    _tag_name(rt.get("Tags", [])),
            "vpc":     rt.get("VpcId", ""),
            "subnets": subnets,
        })
    return rts


def check_ec2(ec2, subnet_ids: list) -> list:
    if not subnet_ids:
        return []
    instances = []
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "subnet-id", "Values": subnet_ids},
            {"Name": "instance-state-name", "Values": ["running", "stopped", "pending"]},
        ]
    )
    for res in resp.get("Reservations", []):
        for i in res.get("Instances", []):
            instances.append({
                "id":     i["InstanceId"],
                "state":  i["State"]["Name"],
                "type":   i.get("InstanceType", ""),
                "name":   _tag_name(i.get("Tags", [])),
                "subnet": i.get("SubnetId", ""),
            })
    return instances


def check_lambda(session, subnet_ids: list) -> list:
    if not subnet_ids:
        return []
    subnet_set = set(subnet_ids)
    lmb = session.client("lambda")
    found = []
    paginator = lmb.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page.get("Functions", []):
            vpc_cfg = fn.get("VpcConfig", {})
            fn_subnets = set(vpc_cfg.get("SubnetIds", []))
            if fn_subnets & subnet_set:
                found.append({
                    "name":  fn["FunctionName"],
                    "state": fn.get("State", "N/A"),
                })
    return found


def check_ecs(session, subnet_ids: list) -> list:
    if not subnet_ids:
        return []
    subnet_set = set(subnet_ids)
    ecs = session.client("ecs")
    found = []

    clusters_resp = ecs.list_clusters()
    cluster_arns = clusters_resp.get("clusterArns", [])

    for cluster_arn in cluster_arns:
        task_arns = []
        paginator = ecs.get_paginator("list_tasks")
        for page in paginator.paginate(cluster=cluster_arn):
            task_arns.extend(page.get("taskArns", []))

        if not task_arns:
            continue

        # describe em lotes de 100
        for i in range(0, len(task_arns), 100):
            batch = task_arns[i:i + 100]
            details = ecs.describe_tasks(cluster=cluster_arn, tasks=batch)
            for task in details.get("tasks", []):
                for att in task.get("attachments", []):
                    for detail in att.get("details", []):
                        if detail.get("name") == "subnetId" and detail.get("value") in subnet_set:
                            found.append({
                                "task_id": task["taskArn"].split("/")[-1],
                                "cluster": cluster_arn.split("/")[-1],
                                "status":  task.get("lastStatus", ""),
                            })
    return found


def check_rds(session, vpc_id: str) -> list:
    rds = session.client("rds")
    found = []
    paginator = rds.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for db in page.get("DBInstances", []):
            if db.get("DBSubnetGroup", {}).get("VpcId") == vpc_id:
                found.append({
                    "id":     db["DBInstanceIdentifier"],
                    "engine": db["Engine"],
                    "status": db["DBInstanceStatus"],
                    "cls":    db["DBInstanceClass"],
                })
    return found


def check_eks(session, vpc_id: str) -> list:
    eks = session.client("eks")
    found = []
    paginator = eks.get_paginator("list_clusters")
    for page in paginator.paginate():
        for name in page.get("clusters", []):
            try:
                desc = eks.describe_cluster(name=name)
                cluster_vpc = desc["cluster"]["resourcesVpcConfig"].get("vpcId", "")
                if cluster_vpc == vpc_id:
                    found.append({"name": name, "vpc": vpc_id})
            except ClientError:
                pass
    return found


def build_usage_report(session, nat_id: str, vpc_id: str) -> UsageReport:
    ec2 = session.client("ec2")
    report = UsageReport()

    log("Verificando route tables...")
    report.route_tables = check_route_tables(ec2, nat_id)

    if not report.route_tables:
        return report  # sem rotas = não está em uso, skip o resto

    all_subnets = list({s for rt in report.route_tables for s in rt["subnets"]})

    log(f"Verificando EC2 nas {len(all_subnets)} subnet(s) afetada(s)...")
    report.ec2_instances = check_ec2(ec2, all_subnets)

    log("Verificando Lambda Functions com VPC config...")
    report.lambdas = check_lambda(session, all_subnets)

    log("Verificando ECS Tasks...")
    report.ecs_tasks = check_ecs(session, all_subnets)

    log(f"Verificando RDS na VPC {vpc_id}...")
    report.rds_instances = check_rds(session, vpc_id)

    log(f"Verificando EKS na VPC {vpc_id}...")
    report.eks_clusters = check_eks(session, vpc_id)

    return report


# ─── Print do relatório de uso ────────────────────────────────────────────────

def print_usage_report(report: UsageReport):
    print(f"\n  {RED}{BOLD}⚠  NAT EM USO — encontrado em {len(report.route_tables)} route table(s):{RESET}")

    for rt in report.route_tables:
        subs = ", ".join(rt["subnets"]) or "(sem subnets associadas)"
        print(f"\n    {YELLOW}Route Table : {rt['id']} ({rt['name']}){RESET}")
        print(f"    VPC         : {rt['vpc']}")
        print(f"    Subnets     : {subs}")

    if report.ec2_instances:
        print(f"\n    {RED}▶ EC2 Instances ({len(report.ec2_instances)}):{RESET}")
        for i in report.ec2_instances:
            print(f"      - {i['id']} [{i['state']}] {i['type']} | {i['name']} | subnet: {i['subnet']}")
    else:
        ok("  Sem instâncias EC2 nas subnets afetadas.")

    if report.lambdas:
        print(f"\n    {RED}▶ Lambda Functions com VPC ({len(report.lambdas)}):{RESET}")
        for fn in report.lambdas:
            print(f"      - {fn['name']} | state: {fn['state']}")
    else:
        ok("  Sem Lambda Functions VPC nas subnets afetadas.")

    if report.ecs_tasks:
        print(f"\n    {RED}▶ ECS Tasks em execução ({len(report.ecs_tasks)}):{RESET}")
        for t in report.ecs_tasks:
            print(f"      - {t['task_id']} [{t['status']}] cluster: {t['cluster']}")

    if report.rds_instances:
        print(f"\n    {YELLOW}▶ RDS na mesma VPC ({len(report.rds_instances)}) — podem depender do NAT para outbound:{RESET}")
        for r in report.rds_instances:
            print(f"      - {r['id']} [{r['status']}] {r['engine']} | {r['cls']}")

    if report.eks_clusters:
        print(f"\n    {RED}▶ EKS Clusters na mesma VPC ({len(report.eks_clusters)}):{RESET}")
        for e in report.eks_clusters:
            print(f"      - {e['name']} | vpc: {e['vpc']}")

    print()
    print(f"  {YELLOW}{BOLD}ATENÇÃO: rotas acima ficarão blackhole após a deleção do NAT.{RESET}")


# ─── Deleção + liberação de EIP ───────────────────────────────────────────────

def delete_nat(session, nat_id: str, eip: str):
    ec2 = session.client("ec2")

    log(f"Deletando NAT Gateway {nat_id}...")
    ec2.delete_nat_gateway(NatGatewayId=nat_id)
    ok(f"NAT Gateway {nat_id} deletado com sucesso.")

    if eip and eip != "sem-eip":
        log(f"Aguardando NAT ser deletado antes de liberar EIP {eip} (60s)...")
        time.sleep(60)
        try:
            addrs = ec2.describe_addresses(Filters=[{"Name": "public-ip", "Values": [eip]}])
            for addr in addrs.get("Addresses", []):
                alloc_id = addr.get("AllocationId")
                if alloc_id:
                    ec2.release_address(AllocationId=alloc_id)
                    ok(f"EIP {eip} ({alloc_id}) liberado.")
        except ClientError as e:
            warn(f"Não foi possível liberar EIP {eip}: {e} — faça manualmente.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NAT Gateway Cleanup — OpenFinance Brasil")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Apenas analisa, sem deletar")
    group.add_argument("--execute", action="store_true", help="Deleta os NATs sem uso")
    args = parser.parse_args()

    dry_run = args.dry_run

    # Banner
    print()
    print(f"{BOLD}╔══════════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║         NAT Gateway Cleanup — OpenFinance Brasil                    ║{RESET}")
    mode_label = f"{YELLOW}DRY-RUN (nenhuma alteração será feita){RESET}{BOLD}" if dry_run else f"{RED}EXECUTE — DELETANDO RECURSOS REAIS{RESET}{BOLD}"
    print(f"{BOLD}║         Mode: {mode_label}      ║{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════════════════════╝{RESET}")
    print()

    if not dry_run:
        print(f"{RED}{BOLD}⚠  ATENÇÃO: Modo EXECUTE ativado!{RESET}")
        print(f"{RED}   Os NAT Gateways SEM uso serão DELETADOS permanentemente.{RESET}")
        confirm = input(f"{YELLOW}   Digite CONFIRMAR para continuar: {RESET}").strip()
        if confirm != "CONFIRMAR":
            print("Abortado.")
            sys.exit(0)
        print()

    # Contadores
    total = safe = in_use = errors = deleted = 0

    for target in NAT_TARGETS:
        total += 1
        sep()
        print(f"{BOLD}NAT Gateway: {CYAN}{target.nat_id}{RESET}  |  {target.name}")
        print(f"  Conta : {target.account_id}   Região: {target.region}")
        print()

        # Assume role
        log(f"Assumindo role na conta {target.account_id}...")
        session = assume_role(target.account_id, target.region)
        if not session:
            err(f"Não foi possível assumir role na conta {target.account_id}. Pulando.")
            errors += 1
            continue

        # Verifica estado do NAT
        log("Verificando estado do NAT Gateway...")
        ec2 = session.client("ec2")
        try:
            resp = ec2.describe_nat_gateways(NatGatewayIds=[target.nat_id])
            nat = resp["NatGateways"][0] if resp["NatGateways"] else None
        except ClientError as e:
            err(f"Erro ao descrever {target.nat_id}: {e}")
            errors += 1
            continue

        if not nat or nat["State"] == "deleted":
            warn(f"NAT {target.nat_id} não encontrado ou já deletado. Pulando.")
            errors += 1
            continue

        nat_state = nat["State"]
        vpc_id    = nat.get("VpcId", "")
        subnet_id = nat.get("SubnetId", "")
        eip       = nat["NatGatewayAddresses"][0].get("PublicIp", "sem-eip") if nat.get("NatGatewayAddresses") else "sem-eip"

        print(f"  Estado : {nat_state}   VPC: {vpc_id}   Subnet: {subnet_id}   EIP: {eip}")
        print()

        # Análise de uso
        try:
            report = build_usage_report(session, target.nat_id, vpc_id)
        except ClientError as e:
            err(f"Erro durante análise de uso: {e}")
            errors += 1
            continue

        if report.in_use:
            in_use += 1
            print_usage_report(report)
        else:
            ok("Nenhuma route table referencia este NAT.")
            safe += 1

        if dry_run:
            status = f"{RED}EM USO — será deletado mesmo assim{RESET}" if report.in_use else f"{GREEN}sem uso{RESET}"
            print(f"\n  {BOLD}RESULTADO [DRY-RUN]: NAT será deletado ({status}{BOLD}).{RESET}")
        else:
            try:
                delete_nat(session, target.nat_id, eip)
                deleted += 1
            except ClientError as e:
                err(f"Falha ao deletar {target.nat_id}: {e}")
                errors += 1

        print()

    # Resumo
    sep()
    print()
    print(f"{BOLD}╔══════════════ RESUMO FINAL ══════════════╗{RESET}")
    print(f"  Total processado    : {total}")
    print(f"  {GREEN}Sem uso             : {safe}{RESET}")
    print(f"  {YELLOW}Em uso (rotas ativas): {in_use}{RESET}")
    if not dry_run:
        print(f"  {GREEN}Deletados          : {deleted}{RESET}")
    print(f"  {YELLOW}Erros/skip         : {errors}{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════╝{RESET}")
    print()

    if dry_run:
        print(f"{GREEN}Para executar a deleção de todos os {total - errors} NATs:{RESET}")
        print(f"  {BOLD}python {sys.argv[0]} --execute{RESET}")
        print()


if __name__ == "__main__":
    main()