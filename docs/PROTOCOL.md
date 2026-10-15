# ZTE F50（UFI 固件）短信接口逆向笔记

本文记录 `f50-sms` 依赖的全部协议细节，来源是设备自带的网页前端
（`js/main.js`、`js/service.js`、`js/util.js`、`js/config/ufi/MU5002/menu.js`、
`js/sms/smslist.js`）以及直接对 `http://192.168.0.1` 的抓包验证。

设备信息（被测机）：

| 项 | 值 |
| --- | --- |
| 网页版本 | `F50_FLYMODEM_ZYV1.0.0B15` |
| 硬件版本 | `MU300HW1.0` |
| 前端配置 | `DEVICE_MODEL="MU5120"`、`DEVICE:"ufi/MU5002"`、`WEB_ATTR_IF_SUPPORT_SHA256: 2` |

---

## 1. 两个入口

```text
GET  /goform/goform_get_cmd_process?isTest=false&cmd=<名字>[&multi_data=1][&其它参数]
POST /goform/goform_set_cmd_process   （application/x-www-form-urlencoded）
       body: isTest=false&goformId=<动作>&...
```

**所有请求都必须带 `Referer`**，否则返回：

```json
{"Error": "none secure connection"}
```

前端用的是 `Referer: http://192.168.0.1/index.html`（由浏览器自动带上），
命令行里手动加同一个头即可。

### 会话是必须的，而且失效时不会报错

读短信同样需要有效的登录会话。**关键陷阱**：会话失效后路由器不返回错误，
而是给出**静默的空响应**，字段悄悄消失：

| 请求 | 会话有效 | 会话失效 |
| --- | --- | --- |
| `cmd=loginfo` | `{"loginfo":"ok"}` | `{"loginfo":"no"}` |
| `cmd=sms_data_total` | `{"messages":[...]}` | `{}`（没有 messages 字段） |
| `cmd=sms_capacity_info` | `{"sms_nv_total":"150",...}` | `{}` |
| `cmd=network_type,network_provider` | 字段齐全 | 只回 `network_provider` |

（无 `Referer` 时才是上面那个 `{"Error":"none secure connection"}`，
两者是**不同**的失败模式，容易混淆。）

因此客户端必须按「响应是否缺少应有字段」判断鉴权，不能把缺字段当成
「没有数据」，否则会安静地报告 0 条短信。

登录的作用范围看起来是**设备级**的：任何客户端登录成功后，其它不带 Cookie
的客户端在会话有效期内也能直接读取。写命令则需要 `JSESSIONID` 与 `AD`。

---

## 2. 登录

仅写操作需要。算法对应前端 `js/util.js` 的 `SHA256` / `cookWithRequest` /
`paswordAlgorithmsCookie`，输出为**大写十六进制**。

```text
1. GET  cmd=LD                     →  {"LD": "19ab27ac-406e-4672-b546-f3c1e7426b8f"}
2. h1 = SHA256(明文密码)                       # 大写 hex
   h2 = SHA256( h1 + LD )                      # 大写 hex
3. POST goformId=LOGIN & password=h2
```

响应：

| `result` | 含义 |
| --- | --- |
| `0` / `4` | 成功 |
| `3` | 密码错误 |
| `2` | 已有用户登录（`duplicateUser`） |
| `5` | 需要重新登录 |

成功时返回 `Set-Cookie: JSESSIONID=...; Path=/goform; HttpOnly`。

> 实测：多次登录、多会话并存，互不踢线；`LOGIN` 之后 `LOGIN` 之外只有
> 写命令受影响。前端还有 `cmd=loginfo`（`{"loginfo":"ok"}` 表示已登录）
> 用来判断状态，但**只读接口无视它**。

---

## 3. 短信读取（本工具的全部依赖）

### 3.1 列表

```text
GET /goform/goform_get_cmd_process
      ?isTest=false
      &cmd=sms_data_total
      &page=0
      &data_per_page=500
      &mem_store=1
      &tags=10
      &order_by=order by id desc
```

响应：

```json
{
  "messages": [
    {
      "id": "57",
      "number": "10690955998",
      "content": "44CQ5Lit5Zu95Yac5Lia6ZO26KGM44CR...",
      "date": "26,10,13,08,30,55,+0800",
      "tag": "1",
      "draft_group_id": "",
      "received_all_concat_sms": "1"
    }
  ]
}
```

字段含义：

| 字段 | 说明 |
| --- | --- |
| `id` | 自增 ID，越大越新。长短信分片会共用同一个 id |
| `number` | 对端号码，发送类短信里可能是 `a;b;` 形式 |
| `content` | **base64(UTF-8)** 的正文 |
| `date` | `YY,MM,DD,hh,mm,ss,+ZZZZ`，例如 `26,10,13,08,30,55,+0800` → 2026-10-13 08:30:55 +08:00 |
| `tag` | 见下表 |
| `draft_group_id` | 草稿分组（同一条草稿发给多个号码时共享） |
| `received_all_concat_sms` | `"1"` 表示长短信已收全；`"0"` 时前端会隐藏该行 |

`tag` 取值（来自 `js/sms/smslist.js` 的渲染分支）：

| tag | 含义 |
| --- | --- |
| `0` | 已读（收件箱） |
| `1` | 未读（收件箱） |
| `2` | 已发送 |
| `3` | 发送失败 |
| `4` | 草稿 |

**坑：`page` / `data_per_page` / `order_by` 被这个固件忽略**，永远返回全库。
只有 `tags=1` 会真的过滤（只返回未读）；`tags=0/2/3/4/10` 行为一致，都是全量。
因此客户端要自己判重、自己排序：

* 按 `id` 归并（同 id 保留 `content` 最长的那个分片，与官方前端一致）；
* 「本页没有新数据」或「返回条数 < data_per_page」就停止翻页。

### 3.2 容量

```text
GET ...?cmd=sms_capacity_info
```

```json
{"sms_nv_total":"150","sms_sim_total":"0",
 "sms_nv_rev_total":57,"sms_nv_send_total":0,"sms_nv_draftbox_total":"0",
 "sms_sim_rev_total":"0","sms_sim_send_total":"0","sms_sim_draftbox_total":"0"}
```

`nv_*` 是设备侧（native），`sim_*` 是 SIM 卡侧；`total` 是容量，
`rev/send/draftbox` 分别是收件 / 发件 / 草稿的占用量。
前端「已用」= 三者相加。

### 3.3 未读数

```text
GET ...?cmd=sms_unread_num,sms_dev_unread_num,sms_sim_unread_num&multi_data=1
```

```json
{"sms_sim_unread_num":"0","sms_dev_unread_num":40,"sts_received_flag":"",
 "sms_unread_num":40,"sms_received_flag":"1"}
```

* `sms_unread_num` = 合计；`sms_dev_unread_num` / `sms_sim_unread_num` 分设备与 SIM。
* `sms_received_flag=1` 表示有新短信，`sts_received_flag=1` 表示有状态报告。
  前端用它们来点亮首页的小图标。

### 3.4 其它相关命令

| cmd | 说明 |
| --- | --- |
| `sms_status_rpt_data` | 状态报告列表，字段结构与 `sms_data_total` 相同 |
| `sms_cmd_status_info&sms_cmd=1` | 短信模块就绪状态，`sms_cmd_status_result=3` 表示就绪 |
| `sms_parameter_info` | 短信参数（服务中心号、有效期等） |
| `pbm_data_total` | 电话本（本机 `HAS_PHONEBOOK=false`，不可用） |

---

## 4. 写命令与 AD 校验

写命令除了会话，还要带 `AD`。前端 `js/service.js` 里的算法：

```js
if (ACCESSIBLE_ID_SUPPORT && needAd && goformId != "LOGIN") {
    var a = SHA256(rd0 + rd1);      // rd0/rd1 在本固件里恒为空串
    var u = getCmd("RD").RD;        // 一次性随机串
    payload.AD = SHA256(a + u);
}
```

即：

```text
AD = SHA256( SHA256("") + RD )        # 大写 hex，RD 来自 cmd=RD
```

相关动作（本工具暂未实现，供后续扩展）：

| goformId | 参数 | 说明 |
| --- | --- | --- |
| `SEND_SMS` | `Number`, `MessageBody`(hex 编码), `sms_time`, `ID`, `encode_type` | 发送 |
| `SAVE_SMS` | `SMSMessage`, `SMSNumber`(分号分隔), `Index`, `draft_group_id` | 存草稿 |
| `DELETE_SMS` | `msg_id="1;2;3;"` | 删除指定短信 |
| `ALL_DELETE_SMS` | `which_cgi="native_inbox"` | 清空收件箱 |
| `SET_MSG_READ` | `msg_id="1;2;"`, `tag=0` | 标记已读 |

发送时的正文编码（前端 `encodeMessage`）：把每个码位转成 4 位十六进制
字符串再拼接（增补平面用 5~6 位），配合 `encode_type`
（`GSM7_default` 或 `UNICODE`，由 `getEncodeType` 判断）。

写命令的**结果要轮询确认**：动作接口只回 `{"result":"success"}`，
真正的执行结果要再查 `cmd=sms_cmd_status_info&sms_cmd=<N>`，
`sms_cmd_status_result` 为 `3` 表示成功、`2` 表示失败，其它值继续等。

---

## 5. 其它可用状态字段

```text
GET ...?cmd=modem_main_state,network_type,network_provider,signalbar,lte_rsrp,rssi,
        Z5g_rsrp,Z5g_SINR,wan_active_band,wan_lte_ca,ppp_status,wan_ipaddr,
        opms_wan_mode,msisdn,sim_msisdn,iccid,imsi,imei,cr_version,wa_version,
        web_version,hardware_version,pin_status&multi_data=1
```

未知命令不会报错，只返回 `{"<cmd>":""}`，因此客户端可以放心批量查询。

---

## 6. 复现步骤

```bash
# 1) 取一次性随机串（注意必须带 Referer）
curl -s -H 'Referer: http://192.168.0.1/index.html' \
  'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=LD'

# 2) 读短信列表（不需要登录）
curl -s -H 'Referer: http://192.168.0.1/index.html' \
  'http://192.168.0.1/goform/goform_get_cmd_process?isTest=false&cmd=sms_data_total&page=0&data_per_page=500&mem_store=1&tags=10&order_by=order%20by%20id%20desc' \
  | jq -r '.messages[] | [.id, .date, .number, (.content|@base64d)] | @tsv'
```
