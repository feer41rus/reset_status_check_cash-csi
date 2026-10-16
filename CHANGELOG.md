# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версии — [SemVer](https://semver.org/lang/ru/).

## [1.0.0] — 2026-10-16

Первый публичный релиз.

### Добавлено
- Вкладка «Сброс статусов»: массовая смена `senttoserverstatus` в `ch_introduction`,
  `ch_purchase`, `ch_reportshift`, `ch_withdrawal` (БД `cash`) и `sent_to_server_status`
  в `loy_transaction` (БД `discount`) за выбранный период.
- Вкладка «Замена shopindex»: массовая замена `shopindex` в `ch_shift` с фильтром
  по периоду и старому значению, экспорт в CSV.
- Режим «Проверить (без изменений)» (dry-run) для обеих вкладок.
- Проверка связи с базами `cash` и `discount` и наличия нужных таблиц.
- Транзакционная обработка: `SELECT … FOR UPDATE`, общий `COMMIT`/`ROLLBACK`
  для обеих баз, `lock_timeout` 30 с.
- Подробные отчёты в `logs/` на каждый запуск.
- Сохранение настроек в `~/.config/reset_status_app/settings.json` (права `600`).
- Сборка в AppImage (`packaging/build_appimage.sh`).

[1.0.0]: https://github.com/feer41rus/reset_status_check_cash-csi/releases/tag/v1.0.0
