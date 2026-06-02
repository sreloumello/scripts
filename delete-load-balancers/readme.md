# delete-load-balancers

Script Python para deletar Load Balancers (ALB/NLB) sem targets em múltiplas contas AWS, com autenticação via AWS SSO + Assume Role.

---

## Pré-requisitos

- Python 3.10+
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html)
- boto3

```bash
pip install boto3
```

---

## Configuração do AWS SSO

O script autentica via uma conta "jump" que tem permissão de assumir roles nas contas alvo. Certifique-se de que seu `~/.aws/config` contém o profile SSO e os profiles das contas alvo, por exemplo:

```ini
[sso-session jump-acct.jump.aws]
sso_start_url = https://sso-suaempresa.awsapps.com/start/#
sso_region = us-east-1
sso_registration_scopes = sso:account:access

[profile jump-acct.jump.aws]
sso_session = jump-acct.jump.aws
sso_account_id = 00000000
sso_role_name = acct-permission
region = us-east-1
output = json
```

> O `ROLE_NAME` definido no script (`luan-martins-role`) deve existir nas contas alvo e ter uma trust policy que permita o assume role a partir da conta jump.

---

## Como usar

### 1. Login no AWS SSO

Autentique na conta jump que tem acesso às demais contas:

```bash
aws sso login --profile jump-acct.jump.aws
```

Isso abrirá o browser para confirmar o login. Após concluir, as credenciais ficam em cache e o boto3 as utiliza automaticamente.

Verifique se o acesso está funcionando:

```bash
aws sts get-caller-identity --profile jump-acct.jump.aws
```

---

### 2. Rodar em Dry-Run

O modo dry-run **não deleta nada**. Ele inspeciona cada Load Balancer e mostra:
- O ARN do LB encontrado
- Os Target Groups associados
- Se há targets registrados ou não (com status de saúde)

```bash
python main.py
```

> Por padrão o script já inicia com `dry_run=True`. Não é necessário alterar nada.

Exemplo de output esperado:

```
--- Conta: 0000000000 | Regiao: us-east-1 | LB: lb-name ---
  Assumindo role: arn:aws:iam::0000000000:role/luan-martins-role
  [DRY-RUN] Deletaria: lb-name (arn:aws:elasticloadbalancing:...)
  [DRY-RUN] Target Group: lb-name-tg | TLS:443
            -> Sem targets registrados (seguro deletar)
```

Se algum LB ainda tiver targets ativos, aparecerá um aviso em `WARNING`:

```
  [DRY-RUN] Target Group: algum-tg | TCP:80
            -> 2 target(s) registrado(s) — ATENCAO!
               * 10.0.1.15:8080 | status: healthy
               * 10.0.1.16:8080 | status: unhealthy | motivo: Target.FailedHealthChecks
```

**Não prossiga para o passo seguinte se algum LB tiver targets ativos.**

---

### 3. Rodar de verdade (deletar)

Após validar o dry-run e confirmar que todos os LBs estão sem targets, edite a última linha do `main.py`:

```python
# Antes:
main(dry_run=True)

# Depois:
main(dry_run=False)
```

Execute:

```bash
python main.py
```

O script irá, para cada Load Balancer:
1. Assumir o role na conta alvo
2. Deletar todos os listeners
3. Deletar o Load Balancer

Exemplo de output:

```
--- Conta: 000000000 | Regiao: us-east-1 | LB: lb-name ---
  Assumindo role: arn:aws:iam::000000000:role/luan-martins-role
  Deletando listener: arn:aws:elasticloadbalancing:...
  Deletando Load Balancer: lb-name (arn:aws:elasticloadbalancing:...)
  Deletado com sucesso: lb-name
```

---

## Estrutura do projeto

```
.
├── main.py       # Script principal
└── README.md     # Este arquivo
```

---

## Permissões IAM necessárias

O role assumido em cada conta alvo precisa das seguintes permissões:

```json
{
  "Effect": "Allow",
  "Action": [
    "elasticloadbalancing:DescribeLoadBalancers",
    "elasticloadbalancing:DescribeListeners",
    "elasticloadbalancing:DescribeTargetGroups",
    "elasticloadbalancing:DescribeTargetHealth",
    "elasticloadbalancing:DeleteListener",
    "elasticloadbalancing:DeleteLoadBalancer"
  ],
  "Resource": "*"
}
```