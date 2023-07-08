# M5stickC Plus 用スマートメーター電力リーダー

BP35A1 を [Wi-SUN HAT](https://www.switch-science.com/products/7612) を使って M5stickC Plus に接続して使います。

M5stickのルートディレクトリに `watt_reader.json` を以下のように保存してください。

```json
{
    "id": "BルートID",
    "password": "Bルートパスワード",
    "pushgateway_url": "pushgatewayのアドレス(ex. http://x.y.z.w:abcd)"
}
```
