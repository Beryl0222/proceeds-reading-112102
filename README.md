# 债券说明文字中的用途承诺与付款复核

募集款受托业务的虚构原条款、申请、凭据和外部付款台账。材料中的人、机构及经营记录是合成的。

每份资料为 UTF-8 JSON 数组。id 在同一份资料内唯一，原始文字按收到的形式保留。

- `data/terms.json`。虚构受托条款文本。rank 越小效力越优先。from 为生效日，source 描述签发来源，text 是原条款。金额采用整数元。
- `data/claims.json`。申请人提交的费用及凭据。部分申请允许按份额独立审议。invoice_id 去重到原凭据，份额不等同于最终用途判定。
- `data/ledger.json`。上期外部签付台账以及银行回单。business_ref 可链接到收到的申请，amount 仅是外部实付或原签金额，文本附有原签解释。
- `data/proofs.json`。送来的合同和凭据补件。invoice_id 连接支付原件。received 为受托人实际收到的时间，contract_scope 保留出具方对凭据的描述。

使用 Python 3.11 标准库，无需安装软件包或连接外部服务。

以下命令分别读取一份原始资料、检查资料中的字段与关联。

```sh
python3 -m json.tool data/terms.json
python3 -m unittest discover -s tests -v
```
