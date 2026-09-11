# TC Scheduler

Decide o que a estação terrestre do SpaceLab rastreia, e quando.

Fecha o circuito que estava aberto entre os dois lados da estação: o operador
cria telecomandos na interface web, que ficavam parados no banco sem ninguém
consumir; e o controle de rotor sabia apontar, mas dependia de um humano para
dizer para onde.

Faz parte da estação terrestre orquestrada em
[nanosat-gs/grs-station](https://github.com/nanosat-gs/grs-station).

## O que faz

1. Busca os satélites ativos com telecomando à espera e dado orbital.
2. Propaga 24 h à frente e prevê as passagens.
3. Pontua por prioridade do comando → quantidade de comandos → elevação máxima,
   e escolhe as que não se sobrepõem (a estação tem um rotor só).
4. No AOS, manda **uma ordem** ao Station Manager, que conduz a passagem
   sozinho até o LOS.
5. Serve o plano por HTTP em `:5591`, para o painel do operador.

## API de leitura

| Método | Caminho |
|---|---|
| `GET` | `/health` |
| `GET` | `/api/station` |
| `GET` | `/api/satellite/<code>` |
| `POST` | `/api/tle/refresh` |

## Instalar e rodar

```bash
pip install -e ".[dev]"
pytest

cp .env.example .env
python -m tc_scheduler.main
```

## Banco

Este serviço é o **único que escreve** no banco da estação — mas o schema é
propriedade do TC Generator. O contrato entre os dois está em
[docs/schema-contract.md](docs/schema-contract.md), e `schema_check.py` o
confere no boot.
