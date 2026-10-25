export function averageWitness(values: number[]): number {
  const count = values.length;
  return count === 0 ? 0 : values.reduce((total, value) => total + value, 0) / count;
}
