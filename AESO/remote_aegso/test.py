import pandas as pd

df_lab = pd.read_csv("aegso_report_pswap1_0/pgen1_0/client_lab_1.csv")
df_teleco = pd.read_csv("aegso_report_pswap1_0/pgen1_0/client_teleco_1.csv")

print("--- LAB (debería ser 20km -> ~100-200 us) ---")
print((df_lab["t_exp_ns"] / 1e3).describe())

print("\n--- TELECO (debería ser 2km -> ~10-20 us) ---")
print((df_teleco["t_exp_ns"] / 1e3).describe())