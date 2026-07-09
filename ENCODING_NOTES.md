# Encoding Notes

## DingTalk Chinese message encoding

Do not send Chinese DingTalk message text by embedding the Chinese body directly in a local Windows PowerShell pipeline, here-string, or complex `ssh "... python -c ..."` command.

That path can corrupt UTF-8 Chinese before the script reaches the server, causing DingTalk users to receive mojibake.

Use one of these safe approaches instead:

1. Generate the Chinese text inside the server-side UTF-8 Python process.
2. If triggering from Windows, keep the transported script ASCII-safe and reconstruct Chinese on the server with Unicode codepoints or escapes.
3. For direct robot messages, keep `msgParam` serialized with `json.dumps({"content": text}, ensure_ascii=False)`.
4. Prefer `send_work_notification` as a verification path when checking Chinese display.
5. Before sending, print/log the final `text` on the server and confirm it is readable Chinese there.

Verified working:

- Server-generated Chinese text sent to Pang Hao displayed normally in DingTalk.
- Enterprise work notification Chinese text sent to Pang Hao displayed normally in DingTalk.

Known bad pattern:

- Local PowerShell here-string with Chinese piped over SSH into `python -`.
- Local PowerShell command string containing Chinese message text passed into remote `python -c`.
