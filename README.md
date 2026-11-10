# Serial Link Test

WindowsのCOMポートで、送信専用、受信専用、交互送受信を試験するPythonスクリプトです。同じPC内のTCPで、実機なしでも試験できます。

## シリアルで試す

```bat
python -m pip install -r requirements.txt
```

両端の `serial_link_test.py` 冒頭で通信条件をそろえます。既定は9600 baud・8N1です。各BAT冒頭の `COM_PORT` を実際の番号に変更し、`serial_receive_sample.bat`、`serial_send_sample.bat` の順に起動します。サイズ、巡数、ウェイト、XON/XOFFは両端でそろえてください。

引数を省略すると、必要な項目を対話入力できます。引数の一覧は `python serial_link_test.py --help` で表示します。サイズは終端を含めて9バイト以上、巡数0は無制限です。

## TCPで試す

次のコマンドを別々のターミナルで実行します。TCPモードはPythonの標準ライブラリで動作します。

```bat
python serial_link_test.py --transport tcp --mode receive --size 64 --count 12 --wait 0.2
python serial_link_test.py --transport tcp --mode send --size 64 --count 12 --wait 0.2
```

交互送受信では両側を `--mode mutual` にし、片側に `--first yes`、もう片側に `--first no` を指定します。

## 表示とログ

通常は各巡の結果を表示します。`--display full` で全受信バイトを画面に、`--dump-rx` でログに出力します。`--display progress` では表示を間引きます。ログは起動時の作業フォルダに保存され、ファイル名には日時とプロセスIDが付きます。

## XON/XOFFと試験データ

試験データは16進8文字の巡番号、A～Zを繰り返すASCIIデータ、末尾の改行で構成します。送受信側とも同じ版を使用してください。受信バイト数はフレームの巡番号と改行を含みます。シリアル読み取りに現れたXON（0x11）とXOFF（0x13）は、フレームの判定やバイト数から除外し、別に集計します。

`--xonxoff yes` のときはOSのシリアルドライバーが送信の停止・再開を担当します。Windowsではドライバーが報告するXOFFによる送信保留と、その解除を監視します。短い保留やドライバー内部で処理された制御文字はアプリで観測できない場合があります。

`--xonxoff software` を指定すると、相手から受け取ったXOFFでアプリが送信を保留し、XONで再開します。BATでは `set "XONXOFF=software"` で選択できます。送信は最大64バイトずつ行い、すでにドライバーに渡した分は送信されることがあります。このモードは相手からの制御文字に応答し、アプリからのXON/XOFF自動送出は行いません。
