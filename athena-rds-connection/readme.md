# Athena ↔ RDS PostgreSQL — Guia de Configuração

Este guia descreve como conectar um banco de dados **Amazon RDS PostgreSQL** ao **Amazon Athena** para permitir consultas federadas diretamente pelo console do Athena.

---

## Como funciona

```
Athena → Lambda (conector) → Glue Connection → RDS PostgreSQL
```

O Athena não se conecta diretamente ao RDS. Ele usa uma **Lambda** como proxy, que lê as credenciais do banco via **Secrets Manager** e executa as queries.

---

## Pré-requisitos — o que precisa ser criado manualmente

### 1. Secret no AWS Secrets Manager

O script **não cria o secret**. Ele precisa existir antes da execução.

**Como criar:**

1. Acesse **AWS Console → Secrets Manager → Store a new secret**
2. Selecione **Credentials for Amazon RDS database**
3. Preencha:
   - **Username:** usuário do banco (ex: `srv_monitoring_athena`)
   - **Password:** senha do banco
   - **Database:** selecione a instância RDS
4. Em **Secret name**, use o mesmo prefixo que será configurado em `SECRET_NAME` no script (ex: `prd/rds/dw/srv_monitoring_athena`)
5. Conclua e copie o **ARN** gerado — você vai precisar dele

> O usuário precisa ter permissão de leitura (`SELECT`) nas tabelas/schemas que serão consultados pelo Athena.

---

### 2. Bucket S3 para Spill

O Athena usa um bucket S3 para armazenar resultados intermediários de queries grandes (spill).

- O bucket já precisa existir
- A Lambda precisa ter permissão de leitura e escrita nele (o script cria essa permissão automaticamente via IAM)
- Anote o nome do bucket — você vai precisar dele

---

### 3. Role de acesso à conta destino (conta jump)

O script usa uma **conta jump** para assumir uma role na conta onde os recursos serão criados. Você precisa estar autenticado na conta jump com permissão de `sts:AssumeRole` para a role configurada.

A role precisa ter as seguintes permissões na conta destino:

- `glue:CreateConnection`, `glue:GetConnection`
- `cloudformation:CreateStack`, `cloudformation:DescribeStacks`, `cloudformation:DeleteStack`
- `lambda:GetFunction`, `lambda:CreateFunction`
- `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`
- `athena:CreateDataCatalog`, `athena:GetDataCatalog`
- `secretsmanager:DescribeSecret`

> Se você já usa a conta jump no dia a dia para acessar a conta destino, sua role provavelmente já tem essas permissões.

---

### 4. Arquivo de template CloudFormation

O arquivo `main.yaml` precisa estar **no mesmo diretório** do script Python.

```
seu-diretorio/
├── main.py   ← script principal
└── create-rds-athena-connection.yaml ← template CFN da Lambda
```

---

## Como executar

### Passo 1 — Instalar dependências

```bash
pip install boto3
```

### Passo 2 — Configurar credenciais AWS

Certifique-se de que suas credenciais AWS estão configuradas:

```bash
aws configure
```

Ou exporte as variáveis de ambiente:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-2
```

### Passo 3 — Preencher as variáveis no script

Abra o arquivo `main.py` e edite o bloco de configuração no topo:

```python
# --- Conta destino ---
TARGET_ACCOUNT_ID = "123456789012"         # ID da conta onde tudo será criado
ROLE_NAME         = "seu-nome-darede"      # nome da sua role na conta destino

# --- RDS / Banco ---
RDS_HOST     = "seu-endpoint.rds.amazonaws.com"   # endpoint do RDS
RDS_PORT     = "5432"
RDS_DATABASE = "postgres"                          # nome do banco
SECRET_ARN   = "arn:aws:secretsmanager:..."        # ARN do secret criado no passo 1
SECRET_NAME  = "prd/rds/dw/srv_monitoring_athena"  # prefixo do secret (sem o hash final)

# --- Rede ---
VPC_ID             = "vpc-..."
SUBNET_IDS         = ["subnet-..."]
SECURITY_GROUP_IDS = ["sg-..."]

# --- Glue Connection ---
GLUE_CONNECTION_NAME = "nome-da-connection"        # nome que aparecerá no Glue

# --- Lambda / CloudFormation ---
LAMBDA_FUNCTION_NAME = "nome-da-lambda"            # nome da Lambda que será criada
CFN_STACK_NAME       = "nome-da-stack"             # nome da stack CloudFormation

# --- Athena ---
SPILL_BUCKET           = "nome-do-bucket-s3"       # bucket para spill (passo 2)
SPILL_PREFIX           = "Unsaved"
ATHENA_DATASOURCE_NAME = "Nome-DataSource-Athena"  # nome que aparecerá no Athena
```

> Todos os recursos (Glue Connection, Lambda, Athena Data Source) serão criados com os nomes que você definir acima.

### Passo 4 — Executar

```bash
python main.py
```

A execução leva entre **3 a 8 minutos** — a maior parte do tempo é aguardar o deploy da stack CloudFormation.

Exemplo de saída esperada:

```
[INFO] ============================================================
[INFO] Iniciando criação de conexão RDS → Athena
[INFO] ============================================================
[INFO] Verificando Glue Connection 'athena-postgres'...
[INFO] Criando Glue Connection...
[INFO] Glue Connection criada com sucesso.
[INFO] Verificando Lambda 'athenafederatedcatalog_athena_postgres'...
[INFO] Lendo template 'create-rds-athena-connection.yaml'...
[INFO] Criando stack CloudFormation 'athena-postgres-dw-connector'...
[INFO]   Status atual: CREATE_IN_PROGRESS
[INFO]   Status atual: CREATE_IN_PROGRESS
[INFO]   Status atual: CREATE_COMPLETE
[INFO] Stack criada com sucesso.
[INFO] Verificando Athena Data Source 'Athena-Postgres'...
[INFO] Criando Athena Data Source 'Athena-Postgres' apontando para Lambda...
[INFO] Athena Data Source criado com sucesso.
[INFO] ============================================================
[INFO] Concluído com sucesso!
[INFO]   Glue Connection : athena-postgres
[INFO]   Lambda          : athenafederatedcatalog_athena_postgres
[INFO]   Athena DS       : Athena-Postgres
[INFO] ============================================================
```

---

## O script é seguro para re-executar

Se algo falhar no meio, pode rodar o script novamente sem problema. Ele verifica se cada recurso já existe antes de criar — recursos já criados são pulados automaticamente.

---

## Verificando o resultado

Após a execução, valide nos seguintes serviços:

| Serviço | O que verificar |
|---|---|
| **AWS Glue → Connections** | Connection com status `READY` |
| **AWS Lambda → Functions** | Lambda com estado `Active` |
| **Amazon Athena → Data sources** | Data source do tipo `LAMBDA` listado |

Para testar uma query no Athena após a criação:

```sql
SELECT *
FROM "nome-do-datasource"."public"."nome-da-tabela"
LIMIT 10;
```

---

## Erros comuns

| Erro | Causa | Solução |
|---|---|---|
| `EntityNotFoundException` no Glue | Secret ou VPC não encontrados | Verifique o `SECRET_ARN` e os IDs de rede |
| `CREATE_FAILED` na stack CFN | Permissão IAM insuficiente | Verifique as permissões da sua conta |
| `MetadataException` no Athena | Data Source já existe com outro tipo | Delete o Data Source existente no console do Athena e re-execute |
| `FileNotFoundError` no template | Arquivo `.yaml` não está no mesmo diretório | Coloque os dois arquivos juntos |