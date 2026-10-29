# 模拟账户改名（D204）

- 来源：用户反馈「开户输入的名称错误，没有地方可以修改」（例：「文-中国银行」应为「文-中国银河」）
- 范围：`/portfolio` 账户卡、`SimAccountRepo`、`LedgerService`
- 纪律：先红后绿；改名不动持仓、成交、现金，也不动指向账户的规则（规则存 `account_id`）

## D204 账户可以改名

现成的三个写入口只有开户、入金/取出、记成交；名字打错只能删户重开，而删户本身还没有。
本轮补一条最小路径：

1. `SimAccountRepo.rename(user_id, account_id, name) -> Account`
   - 名字先 `strip`，空白按 `ValidationError`（与 `open` 同一句「账户名不能为空」）。
   - `(user_id, name)` 唯一键照旧：重名抛 `StorageError ... 已存在`，与开户同一句话。
   - `UPDATE ... WHERE id = ? AND user_id = ?`；改完读回账户行。别人的账户改不到。
2. `LedgerService.rename_account(username, account_id, name) -> Account`
   - 用户名解析成 id（`resolve_actor`），归属先由 `_require_account` 挡（「不存在或不属于用户」）。
   - bump `ledger`：盘中缓存里带着账户名。
3. `POST /portfolio/accounts/<account_id>/rename`（CSRF 保护）
   - 成功 303 → `/portfolio?notice=renamed`，列表闪「账户名已修改。」
   - 业务拒绝回 400，错误在页面顶部，被拒的名字留在那一张卡片的输入框里（D197 的回填纪律）。
4. `/portfolio` 每个账户卡标题旁一个改名表单：输入框预填当前名、按钮「改名」。

不改的东西：账户 id、开户时间、币种、`broker`、`margin_enabled`；也不提供删户（另立一件事）。

## 测试

- 仓库：改名读回新名；重名是 `StorageError`；空白名是 `ValidationError`；别人的账户改不动。
- 服务：别人的账户在建名时就被挡（`StorageError`），行原样。
- 页面：303 + `?notice=renamed` + 新名上屏；空白名/重名 400 且旧名还在；别人的账户 400。

## 登记为待办

- 删户（以及删户前「有持仓/成交时怎么办」的产品决定）。
- 改名留审计行：目前 `sim_accounts` 没有事件表，改名只覆盖 `name`。
