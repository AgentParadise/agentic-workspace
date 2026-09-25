fn main() {
    // Linking this binary needs `cc`, which omni-agent does not ship.
    println!("toolchain-smoke rust-ok {}", 6 * 7);
}
