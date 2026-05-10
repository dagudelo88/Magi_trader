export interface DynamicRiskTier {
  min_score: number | null;
  max_score: number | null;
  multiplier: number;
}

export type DrawdownAction = 'reduce' | 'pause' | 'stop';

export interface RiskSettings {
  base_risk_pct: number;
  dynamic_tiers: DynamicRiskTier[];
  daily_loss_limit_pct: number;
  max_drawdown_pct: number;
  consecutive_loss_limit: number;
  enable_daily_loss_limit: boolean;
  enable_drawdown_protection: boolean;
  enable_consecutive_loss: boolean;
  enable_dynamic_sizing: boolean;
  enable_volatility_pause: boolean;
  volatility_threshold: number | null;
  drawdown_action: DrawdownAction;
  drawdown_reduce_factor: number;
  yolo_mode: boolean;
}

export const DEFAULT_DYNAMIC_TIERS: DynamicRiskTier[] = [
  { min_score: null, max_score: 0.4, multiplier: 0.5 },
  { min_score: 0.4, max_score: 0.7, multiplier: 1 },
  { min_score: 0.7, max_score: 0.85, multiplier: 1.4 },
  { min_score: 0.85, max_score: null, multiplier: 1.75 },
];

export const GLOBAL_RISK_DEFAULTS: RiskSettings = {
  base_risk_pct: 2,
  dynamic_tiers: DEFAULT_DYNAMIC_TIERS,
  daily_loss_limit_pct: 6,
  max_drawdown_pct: 15,
  consecutive_loss_limit: 8,
  enable_daily_loss_limit: true,
  enable_drawdown_protection: true,
  enable_consecutive_loss: true,
  enable_dynamic_sizing: true,
  enable_volatility_pause: false,
  volatility_threshold: null,
  drawdown_action: 'reduce',
  drawdown_reduce_factor: 0.5,
  yolo_mode: false,
};

export function cloneRiskSettings(settings: RiskSettings): RiskSettings {
  return {
    ...settings,
    dynamic_tiers: settings.dynamic_tiers.map((tier) => ({ ...tier })),
  };
}

export function templateRiskDefaults(strategy: string): RiskSettings {
  const isLag = strategy.startsWith('magi_lag_');
  let profile: Pick<
    RiskSettings,
    'base_risk_pct' | 'daily_loss_limit_pct' | 'max_drawdown_pct' | 'consecutive_loss_limit'
  >;

  if (strategy.endsWith('_high')) {
    profile = {
      base_risk_pct: 1.5,
      daily_loss_limit_pct: 5,
      max_drawdown_pct: 12,
      consecutive_loss_limit: 10,
    };
  } else if (strategy.endsWith('_low')) {
    profile = {
      base_risk_pct: 2.8,
      daily_loss_limit_pct: 8,
      max_drawdown_pct: 18,
      consecutive_loss_limit: 6,
    };
  } else {
    profile = {
      base_risk_pct: 2,
      daily_loss_limit_pct: 6,
      max_drawdown_pct: 15,
      consecutive_loss_limit: 8,
    };
  }

  if (isLag) {
    profile = {
      ...profile,
      base_risk_pct: Number((profile.base_risk_pct * 0.85).toFixed(4)),
      daily_loss_limit_pct: Math.max(1, profile.daily_loss_limit_pct - 1),
      max_drawdown_pct: Math.max(1, profile.max_drawdown_pct - 2),
    };
  }

  return {
    ...cloneRiskSettings(GLOBAL_RISK_DEFAULTS),
    ...profile,
  };
}

export function effectiveRiskPct(settings: RiskSettings, consensusScore: number): number {
  const tier =
    settings.dynamic_tiers.find((candidate) => {
      const minOk = candidate.min_score == null || consensusScore >= candidate.min_score;
      const maxOk = candidate.max_score == null || consensusScore < candidate.max_score;
      return minOk && maxOk;
    }) ?? settings.dynamic_tiers[1];
  return settings.base_risk_pct * (tier?.multiplier ?? 1);
}

export function validateRiskSettings(settings: RiskSettings): string | null {
  if (!Number.isFinite(settings.base_risk_pct) || settings.base_risk_pct <= 0) {
    return 'Base risk must be greater than 0%.';
  }
  if (!Number.isFinite(settings.daily_loss_limit_pct) || settings.daily_loss_limit_pct <= 0) {
    return 'Daily loss limit must be greater than 0%.';
  }
  if (!Number.isFinite(settings.max_drawdown_pct) || settings.max_drawdown_pct <= 0) {
    return 'Maximum drawdown must be greater than 0%.';
  }
  if (!Number.isInteger(settings.consecutive_loss_limit) || settings.consecutive_loss_limit <= 0) {
    return 'Consecutive loss limit must be a positive whole number.';
  }
  if (settings.daily_loss_limit_pct > settings.max_drawdown_pct) {
    return 'Daily loss limit should not be higher than maximum drawdown.';
  }
  if (settings.drawdown_action === 'reduce' && (settings.drawdown_reduce_factor <= 0 || settings.drawdown_reduce_factor > 1)) {
    return 'Drawdown reduction factor must be between 0 and 1.';
  }
  if (
    settings.enable_volatility_pause &&
    (settings.volatility_threshold == null || settings.volatility_threshold <= 0)
  ) {
    return 'Volatility threshold is required when volatility pause is enabled.';
  }
  return null;
}

