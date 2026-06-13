module cla4 (
    input a0,
    input a1,
    input a2,
    input a3,
    input b0,
    input b1,
    input b2,
    input b3,
    input cin,
    output s0,
    output s1,
    output s2,
    output s3,
    output cout
);
    wire p0 = a0 ^ b0;
    wire p1 = a1 ^ b1;
    wire p2 = a2 ^ b2;
    wire p3 = a3 ^ b3;

    wire g0 = a0 & b0;
    wire g1 = a1 & b1;
    wire g2 = a2 & b2;
    wire g3 = a3 & b3;

    wire c1 = g0 | (p0 & cin);
    wire c2 = g1 | (p1 & g0) | (p1 & p0 & cin);
    wire c3 = g2 | (p2 & g1) | (p2 & p1 & g0) | (p2 & p1 & p0 & cin);
    wire c4 = g3 | (p3 & g2) | (p3 & p2 & g1) | (p3 & p2 & p1 & g0)
            | (p3 & p2 & p1 & p0 & cin);

    assign s0 = p0 ^ cin;
    assign s1 = p1 ^ c1;
    assign s2 = p2 ^ c2;
    assign s3 = p3 ^ c3;
    assign cout = c4;
endmodule
