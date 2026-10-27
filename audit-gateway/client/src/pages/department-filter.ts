import type { Department } from '@/types/api'

export function filterDepartments (
  departments: Department[],
  keyword: string | null,
): Department[] {
  const needle = keyword?.trim().toLowerCase() ?? ''
  if (!needle) {
    return departments
  }

  return departments.filter(department =>
    [department.name, department.code, department.remark]
      .some(value => value?.toLowerCase().includes(needle)),
  )
}
