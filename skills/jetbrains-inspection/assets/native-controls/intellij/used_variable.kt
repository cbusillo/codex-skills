public fun jbNativeUsedVariableControl(values: List<Int>): Int {
    val count = values.size
    return if (count == 0) 0 else values.sum() / count
}
