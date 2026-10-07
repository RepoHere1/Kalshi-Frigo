import { defineRailway, project, service } from "railway/iac";

export default defineRailway(() => {
  const web = service("web", {
    env: {
      NODE_ENV: "production",
    },
  });

  return project("Kalshi-Frigo", {
    resources: [web],
  });
});
