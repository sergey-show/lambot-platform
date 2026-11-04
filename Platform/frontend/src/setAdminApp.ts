/**
 * Must be imported before `./main`: in ES modules, ordinary entry code after `import` runs
 * after dependencies load, so the "admin" flag would not be set in time.
 */
(window as unknown as { __LAMBOT_APP?: string }).__LAMBOT_APP = "admin";
