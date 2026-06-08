"""
Automatiza a criação completa de uma conexão RDS PostgreSQL no Amazon Athena via:
  1. Glue Connection
  2. Lambda connector (CloudFormation)
  3. Athena Data Source PostgreSQL (connector)
  4. Athena Data Source Lambda (shared connector)
"""

import boto3
import time
import sys

# =============================================================================
# CONFIGURAÇÃO — edite estas variáveis antes de executar
# =============================================================================

REGION = "us-east-2"

# --- Conta destino (onde os recursos serão criados) ---
TARGET_ACCOUNT_ID = "111111111111111"         # ID da conta onde tudo será criado
ROLE_NAME         = "luan-martins-darede"  # nome da role a assumir na conta destino
# ARN montado automaticamente / nao editar
ROLE_ARN = f"arn:aws:iam::{TARGET_ACCOUNT_ID}:role/{ROLE_NAME}"

REGION = "us-east-2"

# --- RDS / Banco ---
RDS_HOST     = "exemplo.fds8gfhiusdkfhjds.us-east-1.rds.amazonaws.com"
RDS_PORT     = "5432"
RDS_DATABASE = "postgres"
SECRET_ARN   = "arn:aws:secretsmanager:us-east-2:111111111111:secret:prd/rds/secret/name-xG6YQH"
SECRET_NAME  = "prd/rds/secret/name"  # prefixo para a policy IAM

# --- Rede ---
VPC_ID            = "vpc-f8sduifhsdpiofsdf"
SUBNET_IDS        = ["subnet-f8sduifhsdpiofsdf"]
SECURITY_GROUP_IDS = ["sg-f8sduifhsdpiofsdf"]

# --- Glue Connection ---
GLUE_CONNECTION_NAME = "athena-postgres-"

# --- Lambda / CloudFormation ---
LAMBDA_FUNCTION_NAME = "athenafederatedcatalog_athena_postgres"
CFN_STACK_NAME       = "athena-postgres"
CFN_TEMPLATE_FILE    = "create-rds-athena-connection.yaml"
IMAGE_URI            = "442426880917.dkr.ecr.us-east-2.amazonaws.com/athena-federation-repository:2026.5.1"

# --- Athena ---
SPILL_BUCKET          = "s3-name"
SPILL_PREFIX          = "Unsaved"
ATHENA_DATASOURCE_NAME = "Athena-Postgres-Name"

# =============================================================================

def log(msg):
    print(f"[INFO] {msg}")


def err(msg):
    print(f"[ERROR] {msg}")
    sys.exit(1)


def assume_role():
    """Assume a role na conta destino e retorna credenciais temporárias."""
    log(f"Assumindo role '{ROLE_ARN}'...")
    sts = boto3.client("sts")
    response = sts.assume_role(
        RoleArn=ROLE_ARN,
        RoleSessionName="create-rds-athena-connection",
        DurationSeconds=3600,
    )
    creds = response["Credentials"]
    log(f"Role assumida com sucesso. Sessão expira em: {creds['Expiration']}")
    return creds


def get_clients():
    """Cria os clientes boto3 usando as credenciais da role assumida."""
    creds = assume_role()
    kwargs = dict(
        region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    return (
        boto3.client("glue",            **kwargs),
        boto3.client("cloudformation",  **kwargs),
        boto3.client("athena",          **kwargs),
        boto3.client("lambda",          **kwargs),
    )


glue, cfn, athena, lmb = get_clients()


# =============================================================================
# PASSO 1 — Glue Connection
# =============================================================================

def create_glue_connection():
    log(f"Verificando Glue Connection '{GLUE_CONNECTION_NAME}'...")
    try:
        glue.get_connection(Name=GLUE_CONNECTION_NAME)
        log("Glue Connection já existe, pulando criação.")
        return
    except glue.exceptions.EntityNotFoundException:
        pass

    log("Criando Glue Connection...")
    glue.create_connection(
        ConnectionInput={
            "Name": GLUE_CONNECTION_NAME,
            "ConnectionType": "POSTGRESQL",
            "ConnectionProperties": {
                "HOST":     RDS_HOST,
                "PORT":     RDS_PORT,
                "DATABASE": RDS_DATABASE,
            },
            "PhysicalConnectionRequirements": {
                "SubnetId":                 SUBNET_IDS[0],
                "SecurityGroupIdList":      SECURITY_GROUP_IDS,
                "AvailabilityZone":         "",
            },
            "AuthenticationConfiguration": {
                "AuthenticationType": "BASIC",
                "SecretArn":          SECRET_ARN,
            },
            "AthenaProperties": {
                "MANAGED_CONNECTION":      "true",
                "spill_bucket":            SPILL_BUCKET,
                "spill_prefix":            SPILL_PREFIX,
                "disable_spill_encryption": "false",
            },
        }
    )
    log("Glue Connection criada com sucesso.")


# =============================================================================
# PASSO 2 — CloudFormation: Lambda connector
# =============================================================================

def deploy_lambda_connector():
    log(f"Verificando Lambda '{LAMBDA_FUNCTION_NAME}'...")
    try:
        lmb.get_function(FunctionName=LAMBDA_FUNCTION_NAME)
        log("Lambda já existe, pulando deploy.")
        return
    except lmb.exceptions.ResourceNotFoundException:
        pass

    log(f"Lendo template '{CFN_TEMPLATE_FILE}'...")
    try:
        with open(CFN_TEMPLATE_FILE, "r") as f:
            template_body = f.read()
    except FileNotFoundError:
        err(f"Template '{CFN_TEMPLATE_FILE}' não encontrado. Coloque-o no mesmo diretório do script.")

    parameters = [
        {"ParameterKey": "LambdaFunctionName",  "ParameterValue": LAMBDA_FUNCTION_NAME},
        {"ParameterKey": "GlueConnection",       "ParameterValue": GLUE_CONNECTION_NAME},
        {"ParameterKey": "SecretName",           "ParameterValue": SECRET_NAME},
        {"ParameterKey": "SpillBucket",          "ParameterValue": SPILL_BUCKET},
        {"ParameterKey": "SubnetIds",            "ParameterValue": ",".join(SUBNET_IDS)},
        {"ParameterKey": "SecurityGroupIds",     "ParameterValue": ",".join(SECURITY_GROUP_IDS)},
    ]

    # Verifica se stack já existe
    try:
        stack = cfn.describe_stacks(StackName=CFN_STACK_NAME)["Stacks"][0]
        status = stack["StackStatus"]
        if status == "CREATE_COMPLETE":
            log(f"Stack '{CFN_STACK_NAME}' já existe e está completa, pulando deploy.")
            return
        elif "ROLLBACK" in status or "FAILED" in status:
            log(f"Stack em estado '{status}', deletando para recriar...")
            cfn.delete_stack(StackName=CFN_STACK_NAME)
            wait_stack("DELETE_COMPLETE")
    except cfn.exceptions.ClientError:
        pass  # stack não existe, prosseguir

    log(f"Criando stack CloudFormation '{CFN_STACK_NAME}'...")
    cfn.create_stack(
        StackName=CFN_STACK_NAME,
        TemplateBody=template_body,
        Capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"],
        Parameters=parameters,
    )

    wait_stack("CREATE_COMPLETE")
    log("Stack criada com sucesso.")


def wait_stack(target_status, timeout=600):
    log(f"Aguardando stack atingir '{target_status}'...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        stack = cfn.describe_stacks(StackName=CFN_STACK_NAME)["Stacks"][0]
        status = stack["StackStatus"]
        log(f"  Status atual: {status}")
        if status == target_status:
            return
        if "FAILED" in status or ("ROLLBACK" in status and target_status != "DELETE_COMPLETE"):
            reason = stack.get("StackStatusReason", "sem detalhes")
            err(f"Stack falhou com status '{status}': {reason}")
        time.sleep(15)
    err(f"Timeout aguardando stack atingir '{target_status}'.")


# =============================================================================
# PASSO 3 — Athena Data Source tipo LAMBDA (shared connector)
# =============================================================================

def create_athena_datasource():
    log(f"Verificando Athena Data Source '{ATHENA_DATASOURCE_NAME}'...")
    try:
        athena.get_data_catalog(Name=ATHENA_DATASOURCE_NAME)
        log("Data Source já existe, pulando criação.")
        return
    except athena.exceptions.MetadataException:
        pass
    except Exception as e:
        if "not found" in str(e).lower() or "does not exist" in str(e).lower():
            pass
        else:
            raise

    lambda_arn = get_lambda_arn()
    log(f"Criando Athena Data Source '{ATHENA_DATASOURCE_NAME}' apontando para Lambda...")

    athena.create_data_catalog(
        Name=ATHENA_DATASOURCE_NAME,
        Type="LAMBDA",
        Description=f"Federated connector para PostgreSQL RDS {RDS_HOST}",
        Parameters={
            "function": lambda_arn,
        },
    )
    log("Athena Data Source criado com sucesso.")


def get_lambda_arn():
    log(f"Obtendo ARN da Lambda '{LAMBDA_FUNCTION_NAME}'...")
    response = lmb.get_function(FunctionName=LAMBDA_FUNCTION_NAME)
    arn = response["Configuration"]["FunctionArn"]
    log(f"Lambda ARN: {arn}")
    return arn


# =============================================================================
# MAIN
# =============================================================================

def main():
    log("=" * 60)
    log("Iniciando criação de conexão RDS → Athena")
    log("=" * 60)

    create_glue_connection()
    deploy_lambda_connector()
    create_athena_datasource()

    log("=" * 60)
    log("Concluído com sucesso!")
    log(f"  Glue Connection : {GLUE_CONNECTION_NAME}")
    log(f"  Lambda          : {LAMBDA_FUNCTION_NAME}")
    log(f"  Athena DS       : {ATHENA_DATASOURCE_NAME}")
    log("=" * 60)


if __name__ == "__main__":
    main()