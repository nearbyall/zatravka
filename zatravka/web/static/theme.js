// Applies the remembered theme before the first paint; without one the system setting rules.
try {
  const theme = localStorage.getItem("theme");
  if (theme) document.documentElement.dataset.theme = theme;
} catch (e) {}
