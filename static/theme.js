// Runs before the page renders so a chosen theme does not flash the system one first.
try {
  const theme = localStorage.getItem("timelog.theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch {}
