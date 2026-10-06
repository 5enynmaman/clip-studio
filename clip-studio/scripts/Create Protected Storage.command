#!/bin/zsh
set -euo pipefail

clip_usb_path='/Volumes/Transcend'
clip_image_path="$clip_usb_path/Private.sparsebundle"

printf '%s\n' 'Создание защищённого хранилища Clip Studio на флешке Transcend.'
printf '%s\n' 'Размер: 10 ГБ; шифрование: AES-256; содержимое флешки сохраняется.'
printf '%s\n' 'Новый пароль вводите только в этом Terminal. При вводе символы не отображаются.'
printf '%s\n' 'Пароль не сохраняется в Clip Studio. Сохраните его у себя: без него образ не открыть.'

if ! /sbin/mount | /usr/bin/grep -Fq " on $clip_usb_path ("; then
  printf '%s\n' 'Флешка Transcend не подключена. Подключите её и запустите этот файл снова.'
  read -r 'clip_reply?Нажмите Enter, чтобы закрыть окно. '
  exit 1
fi
if [[ -e "$clip_image_path" ]]; then
  printf '%s\n' 'Private.sparsebundle уже существует. Создание остановлено, чтобы сохранить его.'
  read -r 'clip_reply?Нажмите Enter, чтобы закрыть окно. '
  exit 1
fi

if ! /usr/sbin/diskutil image create blank --encrypt --format UDSB --size 10g --volumeName 'Clip Vault' --fs APFS "$clip_image_path"; then
  printf '%s\n' 'Создание не завершилось. Вернитесь в Codex и сообщите об ошибке; существующие файлы не удаляйте.'
  read -r 'clip_reply?Нажмите Enter, чтобы закрыть окно. '
  exit 1
fi

printf '%s\n' 'Образ создан. Для открытия может потребоваться повторно ввести тот же пароль.'
/usr/bin/hdiutil attach "$clip_image_path"
printf '%s\n' 'Готово. Вернитесь в Codex и напишите «готово», чтобы перенести видео и подключить приложение.'
read -r 'clip_reply?Нажмите Enter, чтобы закрыть окно. '
