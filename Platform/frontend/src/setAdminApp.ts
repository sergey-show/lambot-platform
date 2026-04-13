/**
 * Должен импортироваться до `./main`: в ES-модулях обычный код в entry после `import` выполняется
 * после загрузки зависимостей, из-за чего флаг «admin» не успевал установиться.
 */
(window as unknown as { __LAMBOT_APP?: string }).__LAMBOT_APP = "admin";
